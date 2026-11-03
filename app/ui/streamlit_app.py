"""Streamlit 화면 (PRD v1.3 3.5). 실행: streamlit run app/ui/streamlit_app.py

- 데이터 조회·필터·이벤트 변환은 app/ui/data.py, 출력 형식은 app/export/formatter.py만 쓴다.
- APP_MODE=viewer(Streamlit Cloud)에서는 DB를 읽기 전용으로 열고 '지금 수집 실행'을 숨긴다.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from streamlit_calendar import calendar  # noqa: E402

from app.config import get_config  # noqa: E402
from app.export.formatter import format_lines, rows_to_csv  # noqa: E402
from app.pipeline.reconcile import CONFIRMED, MISMATCH, MISSING, UNCONFIRMED  # noqa: E402
from app.storage import db  # noqa: E402
from app.ui import data as D  # noqa: E402

TRANSPARENCY_NOTICE = "본 자료는 공개 기사만을 출처로 수집·정리한 것입니다"  # PRD 6장
LOCK_PATH = ROOT / "data" / ".lock"
REASON_LABEL = {"initial": "최초 채택", "multi_to_single": "복수 발표 → 개별 기사 확인",
                "history_to_original": "이력정리 → 원본 확인", "earlier_article": "더 이른 게재일 기사 확인"}
RECONCILE_ICON = {CONFIRMED: "✅", MISMATCH: "⚠", UNCONFIRMED: "⏳", MISSING: "❗"}

st.set_page_config(page_title="제강사 철스크랩 단가발표 이력", page_icon="📅", layout="wide")
st.markdown("""<style>
.block-container {padding-top: 1.2rem; padding-bottom: 3.2rem;}
.transparency-notice {position: fixed; left: 0; right: 0; bottom: 0; z-index: 999; padding: .35rem 1rem;
  text-align: center; font-size: .85rem; background: var(--background-color, #fff); border-top: 1px solid rgba(128,128,128,.3);}
.legend {display: flex; flex-wrap: wrap; gap: .35rem .9rem; align-items: center; font-size: .85rem; margin: .2rem 0 .5rem;}
.legend .sw {display: inline-block; width: .9rem; height: .9rem; border-radius: 3px; vertical-align: -2px; margin-right: .25rem;}
.badge {display: inline-block; padding: .05rem .45rem; margin: 0 .25rem .25rem 0; border-radius: .6rem; font-size: .78rem;
  background: rgba(128,128,128,.15);}
.badge.warn {background: #fde2e2; color: #9b1c1c;} .badge.wait {background: #fff3cd; color: #7a5b00;}
</style>""", unsafe_allow_html=True)

CALENDAR_CSS = """
.fc-event { border-width: 2px !important; cursor: pointer; }
.fc-event.ev-history { border-style: dashed !important; }
.fc-event-title { font-weight: 600; font-size: .74rem; letter-spacing: -.02em; white-space: nowrap; }
.fc-daygrid-event { padding: 0 1px; }
.fc .fc-toolbar-title { font-size: 1.05rem; }
.fc .fc-daygrid-day-number { font-size: .8rem; }
.fc .fc-button { padding: .2rem .55rem; font-size: .85rem; }
"""

cfg = get_config()
viewer_mode = cfg.settings.app.mode == "viewer"


def get_conn():
    # SQLite 연결은 스레드마다 따로 써야 하므로(Streamlit은 실행마다 스레드가 다름) 캐시하지 않고 매번 연다
    try:
        return db.connect(db.get_db_path(), readonly=viewer_mode)
    except FileNotFoundError:
        return None


conn = get_conn()

# ---------------------------------------------------------------- 헤더
runs = D.recent_runs(conn, 1) if conn else []
last_label = (runs[0]["ended_at"] or runs[0]["started_at"])[:16].replace("T", " ") if runs else "수집 기록 없음"
h1, h2 = st.columns([3, 1])
h1.markdown("### 제강사 철스크랩 단가발표 이력")
h2.markdown(f"<div style='text-align:right;padding-top:.6rem'>마지막 수집: <b>{last_label}</b></div>",
            unsafe_allow_html=True)

if conn is None:
    st.warning(f"DB 파일이 없습니다: {db.get_db_path()} — 수집을 먼저 실행하거나 SCRAP_DB_PATH를 확인하세요.")
    st.markdown(f"<div class='transparency-notice'>{TRANSPARENCY_NOTICE}</div>", unsafe_allow_html=True)
    st.stop()

# ---------------------------------------------------------------- 사이드바 필터 (3.5.4)
names = cfg.steelmakers.names()
with st.sidebar:
    st.header("필터")
    period = st.date_input("기간", value=(cfg.settings.period.default_from, cfg.settings.period.default_to),
                           format="YYYY-MM-DD", key="period")
    if isinstance(period, (tuple, list)) and len(period) == 2:
        d_from, d_to = period
    else:  # 시작일만 고른 상태
        d_from = d_to = period[0] if isinstance(period, (tuple, list)) else period
    makers = st.multiselect("제강사", names, default=names, key="makers")
    basis = st.radio("날짜 기준", ["적용일자", "기사발표일"], horizontal=True, key="basis")
    st.caption("구분")
    c1, c2, c3 = st.columns(3)
    directions = {k for k, on in (("up", c1.checkbox("인상", True, key="d_up")),
                                  ("down", c2.checkbox("인하", True, key="d_down")),
                                  ("mixed", c3.checkbox("혼합", True, key="d_mixed"))) if on}
    include_history = st.checkbox("이력정리기사 채택분 포함", True, key="hist")

    if not viewer_mode:
        st.divider()
        run_clicked = st.button("지금 수집 실행", type="primary", width="stretch",
                                disabled=LOCK_PATH.exists(), help="최근 3일 일일 수집을 실행합니다 (몇 분 걸림)")
        if LOCK_PATH.exists():
            st.info("수집이 실행 중입니다.")
    else:
        run_clicked = False

filters = D.Filters(d_from, d_to, set(makers), "published" if basis == "기사발표일" else "apply", directions,
                    include_history)
all_rows = D.load_adopted(conn)
rows = D.apply_filters(all_rows, filters)

# ---------------------------------------------------------------- '지금 수집 실행' (3.6)
if run_clicked:
    with st.status("수집 실행 중… (창을 닫지 마세요)", expanded=True) as status_box:
        log_area = st.empty()
        lines: list[str] = []
        proc = subprocess.Popen([sys.executable, "-m", "app.pipeline.run", "--mode", "manual"], cwd=str(ROOT),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env={**__import__("os").environ,
                                                                                     "PYTHONIOENCODING": "utf-8"})
        assert proc.stdout is not None
        for raw in proc.stdout:
            lines.append(raw.decode("utf-8", errors="replace").rstrip())
            log_area.code("\n".join(lines[-30:]), language=None)
        rc = proc.wait()
        labels = {0: ("완료", "complete"), 2: ("일부 완료(partial)", "complete"), 3: ("다른 실행이 진행 중", "error")}
        text, state = labels.get(rc, ("실패", "error"))
        status_box.update(label=f"수집 {text} (종료 코드 {rc})", state=state, expanded=rc not in (0, 2))
    st.button("결과 반영하기", on_click=lambda: None)

# ---------------------------------------------------------------- 탭
tab_cal, tab_out, tab_status = st.tabs(["달력", "목록·출력", "데이터 상태"])


def legend_html() -> str:
    parts = [f"<span><span class='sw' style='background:{s.color}'></span>{s.name}</span>"
             for s in cfg.steelmakers.steelmakers]
    parts += ["<span>▲ 인상 <span class='sw' style='border:2px solid #d62728'></span></span>",
              "<span>▼ 인하 <span class='sw' style='border:2px solid #1f4fd6'></span></span>",
              "<span>◆ 혼합 <span class='sw' style='border:2px solid #7f7f7f'></span></span>",
              "<span><span class='sw' style='border:2px dashed #555'></span>이력정리기사 채택</span>",
              "<span>⏳ 원본 확인 중</span>", "<span>⚠ 검토 필요·충돌</span>"]
    return "<div class='legend'>" + "".join(parts) + "</div>"


def reason_text(reason: str | None) -> str:
    """'교체됨: multi_to_single' → '교체됨: 복수 발표 → 개별 기사 확인'"""
    if not reason:
        return ""
    head, _, code = reason.partition(": ")
    return f"{head}: {REASON_LABEL[code]}" if code in REASON_LABEL else reason


def badges(row: dict) -> str:
    cls = {"검토 필요": "warn", "충돌": "warn", "원본 확인 중": "wait"}
    return "".join(f"<span class='badge {cls.get(f, '')}'>{f}</span>" for f in D.flags(row))


def render_detail(ann_id: int) -> None:
    row = D.load_announcement(conn, ann_id)
    if row is None:
        st.info("선택한 발표를 찾을 수 없습니다.")
        return
    info = D.detail(row)
    st.subheader(f"{info['제강사']} · {info['적용일자']}")
    st.markdown(f"**{info['발표내용']}**  \n{badges(row)}", unsafe_allow_html=True)
    if any(i.get("plant") for i in row["items"]):
        st.dataframe(pd.DataFrame([{"공장": i.get("plant") or "공통", "등급": i["grade"],
                                    "변동(원/kg)": f"{int(i['change_krw_per_kg']):+d}", "비고": i.get("note") or ""}
                                   for i in row["items"]]), hide_index=True, width="stretch")
    st.markdown("\n".join(f"- **{k}**: {v}" for k, v in info.items() if k not in ("제강사", "적용일자", "발표내용")))
    st.link_button("기사 열기 ↗", row["url"], width="stretch")

    group = [g for g in D.load_group(conn, row["dedup_key"]) if g["id"] != row["id"]]
    if group:
        st.markdown("**같은 발표를 다룬 다른 기사**")
        st.dataframe(pd.DataFrame([{"매체": g["media_name"], "기사발표일": g["published_date"],
                                    "유형": D.TYPE_LABEL.get(g["source_type"], g["source_type"]),
                                    "상태": D.STATUS_LABEL.get(g["status"], g["status"]),
                                    "사유": reason_text(g["reject_reason"]), "기사": g["url"]} for g in group]),
                     hide_index=True, width="stretch",
                     column_config={"기사": st.column_config.LinkColumn("기사", display_text="열기")})
    hist = D.load_adoption_history(conn, row["dedup_key"])
    if len(hist) > 1 or (hist and hist[0]["reason"] != "initial"):
        st.markdown("**교체 이력**")
        st.dataframe(pd.DataFrame([{"일시": (h["changed_at"] or "")[:16].replace("T", " "),
                                    "사유": REASON_LABEL.get(h["reason"], h["reason"]),
                                    "이전": h["old_media"] or "-", "이후": h["new_media"]} for h in hist]),
                     hide_index=True, width="stretch")


# ---------------------------------------------------------------- 달력 탭 (3.5.2, 3.5.3)
with tab_cal:
    st.markdown(legend_html(), unsafe_allow_html=True)
    cal_col, detail_col = st.columns([3, 2], gap="medium")
    events = D.to_events(rows, cfg, filters.date_basis)
    with cal_col:
        options = {"initialView": "dayGridMonth", "initialDate": filters.date_from.isoformat(), "locale": "ko",
                   "height": 600, "dayMaxEvents": 4, "fixedWeekCount": False,
                   "headerToolbar": {"left": "prev,next today", "center": "title", "right": "dayGridMonth,listMonth"},
                   "buttonText": {"today": "오늘", "month": "월", "list": "목록"}}
        cal_key = f"cal-{filters.date_from}-{filters.date_basis}-{len(events)}-{hash(tuple(e['id'] for e in events))}"
        result = calendar(events=events, options=options, custom_css=CALENDAR_CSS, callbacks=["eventClick"],
                          key=cal_key)
        st.caption(f"표시 {len(events)}건 · 날짜 기준: {basis}")
    if isinstance(result, dict) and result.get("callback") == "eventClick":
        st.session_state["selected_ann"] = int(result["eventClick"]["event"]["id"])
    with detail_col:
        sel = st.session_state.get("selected_ann")
        if sel and any(r["id"] == sel for r in rows):
            render_detail(sel)
        else:
            st.info("달력에서 발표를 클릭하면 상세 내용과 기사 링크가 여기에 표시됩니다.")

# ---------------------------------------------------------------- 목록·출력 탭 (3.5.5)
with tab_out:
    out_rows = D.to_output_rows(rows, cfg)
    st.markdown(f"**채택 발표 {len(out_rows)}건** — 적용일자 오름차순, 제강사 순")
    if out_rows:
        st.dataframe(pd.DataFrame([dict(zip(["제강사명", "적용일자", "발표내용", "기사발표일", "기사링크"], r.columns()))
                                   for r in out_rows]), hide_index=True, width="stretch")
        st.markdown("**출력 형식 (오른쪽 위 버튼으로 복사)**")
        st.code(format_lines(out_rows), language=None)
        include_header = st.checkbox("CSV에 헤더 포함", False, key="csv_header")
        st.download_button("CSV 다운로드", data=rows_to_csv(out_rows, include_header),
                           file_name=f"scrap_price_{filters.date_from}_{filters.date_to}.csv", mime="text/csv",
                           key="csv_download")
    else:
        st.info("조건에 맞는 채택 발표가 없습니다.")

# ---------------------------------------------------------------- 데이터 상태 탭 (3.5.7)
with tab_status:
    recent = D.recent_runs(conn, 10)
    if recent:
        last = recent[0]
        stt = last["stats"]
        llm = stt.get("llm") or {}
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("마지막 실행", last_label[5:] if len(last_label) > 5 else last_label)
        m2.metric("상태", {"success": "정상", "partial": "일부 완료", "failed": "실패"}.get(last["status"], "실행 중"))
        m3.metric("LLM 호출", f"{llm.get('calls', 0)}건", f"오류 {llm.get('errors', 0)}건", delta_color="off",
                  delta_arrow="off")
        m4.metric("수집 기사", (stt.get("articles") or {}).get("listed", "-"))
        m5.metric("로그인", {"logged_in": "정상", "auth_required": "로그인 필요"}.get(stt.get("auth"), "-"))
        if stt.get("auth") == "auth_required":
            st.error("Claude Code 로그인이 풀렸습니다. 회사 PC에서 `claude auth login` 후 다시 실행하세요.")
        st.markdown("**누락 점검** (매체·키워드별 가장 오래된 수집 기사, 한도 도달)")
        ms = D.media_stats_rows(last, cfg)
        if ms:
            if any(r["누락 가능"] for r in ms):
                st.warning("⚠ 표시된 매체는 페이지 한도에 걸려 기간 앞부분이 빠졌을 수 있습니다.")
            st.dataframe(pd.DataFrame(ms), hide_index=True, width="stretch")
        if stt.get("errors"):
            with st.expander(f"오류 {len(stt['errors'])}건"):
                st.code("\n".join(stt["errors"]), language=None)
        with st.expander("최근 실행 기록"):
            st.dataframe(pd.DataFrame([{"시작": r["started_at"][:16].replace("T", " "), "모드": r["mode"],
                                        "기간": f"{r['date_from'] or ''} ~ {r['date_to'] or ''}", "상태": r["status"],
                                        "LLM 호출": (r["stats"].get("llm") or {}).get("calls")} for r in recent]),
                         hide_index=True, width="stretch")
    else:
        st.info("실행 기록이 없습니다.")

    st.markdown("**이력정리 기사 대조** (인수 기준 1번)")
    rep = D.reconcile_report(conn)
    if rep.total:
        s = rep.summary()
        st.markdown(f"대조 대상 **{s['대조 대상']}**건 / 원본 확인 {s[CONFIRMED]} · 불일치 {s[MISMATCH]} · "
                    f"원본 미확인 {s[UNCONFIRMED]} · 누락 {s[MISSING]} — 누락률 **{rep.gap_rate * 100:.1f}%**")
        st.dataframe(pd.DataFrame([{"결과": f"{RECONCILE_ICON[e.result]} {e.result}", "제강사": e.steelmaker,
                                    "적용일자": e.apply_date, "이력정리 기사 내용": e.content_text} for e in rep.entries]),
                     hide_index=True, width="stretch")
    else:
        st.caption("이력정리 기사가 아직 없습니다.")

    st.markdown("**검토 필요·충돌**")
    rv = D.review_list(conn)
    if rv:
        st.dataframe(pd.DataFrame(rv), hide_index=True, width="stretch",
                     column_config={"기사": st.column_config.LinkColumn("기사", display_text="열기")})
    else:
        st.caption("없음")

    st.markdown("**원본 확인 대기** (이력정리 기사만 있어 원본 기사를 다시 찾는 중)")
    pv = D.pending_verify(conn)
    if pv:
        st.dataframe(pd.DataFrame([{"제강사": p["steelmaker"], "적용일자": p["apply_date"], "시도": p["attempts"],
                                    "마지막 검색": (p["last_searched_at"] or "")[:16].replace("T", " ")} for p in pv]),
                     hide_index=True, width="stretch")
    else:
        st.caption("없음")

    with st.expander("전체 수집 기사 (none·사전 필터 포함, 수집 근거)"):
        al = D.articles_list(conn)
        if al:
            st.dataframe(pd.DataFrame(al), hide_index=True, width="stretch",
                         column_config={"링크": st.column_config.LinkColumn("링크", display_text="열기")})
        else:
            st.caption("없음")

# ---------------------------------------------------------------- 하단 고정 문구 (6장)
st.markdown(f"<div class='transparency-notice'>{TRANSPARENCY_NOTICE}</div>", unsafe_allow_html=True)
