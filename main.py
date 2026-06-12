# -*- coding: utf-8 -*-
"""
동행파워볼(랜덤볼) Android APK용 Kivy 앱 v3

핵심 방식
- Selenium/Tkinter/XPath 클릭을 사용하지 않습니다. Android에서는 Selenium 브라우저 제어가 맞지 않습니다.
- bepick 결과표 HTML을 requests로 직접 읽고, API 패턴 데이터를 보조로 합쳐 4개 항목 픽을 생성합니다.
- 4개 항목: 파워볼-홀짝 / 파워볼-언더오버 / 일반볼-홀짝 / 일반볼-언더오버
- 회차 결과가 표에 올라오면 자동으로 적중/미적중 판정합니다.

주의
- 랜덤 결과는 100% 적중을 보장할 수 없습니다.
- 화면의 “분석완료 100%”는 4개 항목 분석 절차가 모두 완료됐다는 뜻입니다.
- 본 앱은 과거 데이터 기반 자동 분석/검증용입니다.
"""

from __future__ import annotations

import os
os.environ.setdefault("KIVY_LOG_LEVEL", "info")

import csv
import html
import logging
import re
import threading
import time
import webbrowser
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

# requests/urllib3 디버그 로그 숨김: 콘솔에는 앱 상태 로그만 보이게 정리합니다.
logging.basicConfig(level=logging.WARNING)
for _logger_name in ("urllib3", "requests", "charset_normalizer", "chardet"):
    logging.getLogger(_logger_name).setLevel(logging.WARNING)

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.properties import BooleanProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.logger import Logger

Logger.setLevel(logging.INFO)

try:
    from kivy.utils import platform
except Exception:
    platform = "unknown"

APP_TITLE = "동행파워볼 APK 자동픽 v3"
GAME_URL = "https://bepick.net/game.bp#/game/default/dhrpowerball"
RESULT_URL = "https://bepick.net/game/default/dhrpowerball"
LIVE_URL = "https://bepick.net/json/game/dhrpowerball.json"
PATTERN_URL = (
    "https://bepick.net/api/get_pattern/dhrpowerball/rounds/"
    "{ptype}/20/{base_round}/{start_date}/{end_date}?_={ts}"
)

# 일반볼 합계 기준: 72 이하는 언더, 73 이상은 오버
NORMAL_UNDER_MAX = 72

ITEMS: Dict[str, Dict[str, object]] = {
    "fd1": {"name": "파워볼-홀짝", "labels": {1: "홀", 2: "짝"}},
    "fd2": {"name": "파워볼-언더오버", "labels": {1: "언더", 2: "오버"}},
    "fd3": {"name": "일반볼-홀짝", "labels": {1: "홀", 2: "짝"}},
    "fd4": {"name": "일반볼-언더오버", "labels": {1: "언더", 2: "오버"}},
}


@dataclass
class Prediction:
    ptype: str
    name: str
    target_round: int
    pick_code: int
    pick_label: str
    confidence: float
    grade: str
    detail: str
    source: str


@dataclass
class ResultRow:
    date: str
    round_no: int
    powerball: int
    normal_nums: List[int]
    normal_sum: int
    raw: str


class DhrAnalyzer:
    def __init__(self, user_data_dir: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Referer": GAME_URL,
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 13; Mobile) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Mobile Safari/537.36"
                ),
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        self.start_date = "20241020"
        self.last_table_fingerprint = ""
        self.pending_predictions: Dict[str, Prediction] = {}
        self.last_analyzed_round: Optional[int] = None
        # 회차별 판정 팝업이 반복으로 뜨지 않도록 메모리 잠금합니다.
        self.notified_rounds: set[int] = set()
        self.history_csv = os.path.join(user_data_dir, "dhrpowerball_history.csv")

    @staticmethod
    def now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def end_date() -> str:
        return datetime.now().strftime("%Y%m%d")

    @staticmethod
    def estimate_round() -> int:
        now = datetime.now()
        return (now.hour * 60 + now.minute) // 5 + 1

    def fetch_json(self, url: str, timeout: int = 8) -> dict:
        res = self.session.get(url, timeout=timeout)
        res.raise_for_status()
        return res.json()

    def fetch_live(self) -> dict:
        return self.fetch_json(f"{LIVE_URL}?_={self.now_ms()}")

    def fetch_pattern(self, ptype: str, base_round: int) -> dict:
        url = PATTERN_URL.format(
            ptype=ptype,
            base_round=max(1, int(base_round)),
            start_date=self.start_date,
            end_date=self.end_date(),
            ts=self.now_ms(),
        )
        return self.fetch_json(url)

    @staticmethod
    def html_to_text(raw_html: str) -> str:
        if not raw_html:
            return ""
        s = str(raw_html)
        s = re.sub(r"(?is)<script[^>]*>.*?</script>", "\n", s)
        s = re.sub(r"(?is)<style[^>]*>.*?</style>", "\n", s)
        s = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", "\n", s)
        s = re.sub(r"(?i)</(?:tr|li|p|div|h[1-6]|section|article|tbody|thead|table)>", "\n", s)
        s = re.sub(r"(?i)<br\s*/?>", "\n", s)
        s = re.sub(r"<[^>]+>", " ", s)
        s = html.unescape(s).replace("\xa0", " ")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in s.replace("\r", "\n").split("\n")]
        return "\n".join(line for line in lines if line)

    def fetch_result_page_text(self) -> Tuple[str, str]:
        last_error = "unknown"
        for url in (RESULT_URL, GAME_URL):
            try:
                res = self.session.get(url, timeout=8)
                res.raise_for_status()
                text = self.html_to_text(res.text)
                if text.strip():
                    return text, url
            except Exception as exc:
                last_error = str(exc)
        return "", last_error

    def parse_result_rows(self, text: str) -> List[ResultRow]:
        if not text:
            return []

        rows: List[ResultRow] = []
        seen = set()
        lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]

        date_round_re = re.compile(r"^(20\d{2}[-.]\d{2}[-.]\d{2})\s*-\s*(\d{1,3})\b")
        nums_re = re.compile(
            r"\b([0-9])\s+([A-F])\s+"
            r"(\d{1,2})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s+"
            r"(\d{1,3})\s+([A-F])\b"
        )

        for idx, line in enumerate(lines):
            m = date_round_re.search(line)
            if not m:
                continue
            date_s = m.group(1).replace(".", "-")
            round_no = int(m.group(2))
            block = " ".join(lines[idx: min(len(lines), idx + 14)])
            nm = nums_re.search(block)
            if not nm:
                continue
            row = self._make_result_row(date_s, round_no, nm, block)
            key = (row.date, row.round_no, row.powerball, tuple(row.normal_nums), row.normal_sum)
            if key not in seen:
                seen.add(key)
                rows.append(row)

        if len(rows) >= 8:
            return rows

        flat = re.sub(r"\s+", " ", text)
        flat_re = re.compile(
            r"(20\d{2}[-.]\d{2}[-.]\d{2})\s*-\s*(\d{1,3})\s*"
            r"(?:\(\s*\d+\s*\))?\s*"
            r"(?:\d{1,2}:\d{2}(?::\d{2})?)?\s*"
            r"([0-9])\s+([A-F])\s+"
            r"(\d{1,2})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s+"
            r"(\d{1,3})\s+([A-F])"
        )
        for m in flat_re.finditer(flat):
            date_s = m.group(1).replace(".", "-")
            round_no = int(m.group(2))
            powerball = int(m.group(3))
            normal_nums = [int(m.group(i)) for i in range(5, 10)]
            normal_sum_page = int(m.group(10))
            normal_sum_calc = sum(normal_nums)
            normal_sum = normal_sum_calc if normal_sum_calc != normal_sum_page else normal_sum_page
            row = ResultRow(date_s, round_no, powerball, normal_nums, normal_sum, m.group(0)[:260])
            key = (row.date, row.round_no, row.powerball, tuple(row.normal_nums), row.normal_sum)
            if key not in seen:
                seen.add(key)
                rows.append(row)
        return rows

    @staticmethod
    def _make_result_row(date_s: str, round_no: int, nm: re.Match, raw: str) -> ResultRow:
        powerball = int(nm.group(1))
        normal_nums = [int(nm.group(i)) for i in range(3, 8)]
        normal_sum_page = int(nm.group(8))
        normal_sum_calc = sum(normal_nums)
        normal_sum = normal_sum_calc if normal_sum_calc != normal_sum_page else normal_sum_page
        return ResultRow(date_s, round_no, powerball, normal_nums, normal_sum, raw[:260])

    @staticmethod
    def rows_to_sequences(rows: List[ResultRow]) -> Dict[str, List[int]]:
        if not rows:
            return {}
        ordered = sorted(rows, key=lambda r: (r.date, r.round_no))
        seq = {"fd1": [], "fd2": [], "fd3": [], "fd4": []}
        for row in ordered:
            pb = row.powerball
            total = row.normal_sum
            seq["fd1"].append(1 if pb % 2 == 1 else 2)
            seq["fd2"].append(1 if pb <= 4 else 2)
            seq["fd3"].append(1 if total % 2 == 1 else 2)
            seq["fd4"].append(1 if total <= NORMAL_UNDER_MAX else 2)
        return {k: v[-288:] for k, v in seq.items() if len(v) >= 8}

    def fetch_table_sequences(self) -> Tuple[Dict[str, List[int]], List[ResultRow], str, str]:
        text, source = self.fetch_result_page_text()
        rows = self.parse_result_rows(text)
        if len(rows) < 8:
            return {}, rows, source, f"결과표 파싱 실패: rows {len(rows)}"

        sequences = self.rows_to_sequences(rows)
        latest = max(rows, key=lambda r: (r.date, r.round_no))
        first = min(rows, key=lambda r: (r.date, r.round_no))
        fingerprint = f"{latest.date}|{latest.round_no}|{latest.powerball}|{latest.normal_sum}|{len(rows)}"
        if fingerprint != self.last_table_fingerprint:
            self.last_table_fingerprint = fingerprint
            log = (
                f"결과표 성공: rows {len(rows)} / 범위 {first.round_no}~{latest.round_no}회 / "
                f"최신 {latest.round_no}회 / PB {latest.powerball} / 일반볼합 {latest.normal_sum}"
            )
        else:
            log = "동기화 완료"
        return sequences, rows, source, log

    def expand_pattern_sequence(self, data: dict) -> List[int]:
        seq: List[int] = []
        for item in data.get("list", []) or []:
            try:
                result = int(item.get("result"))
                keep = max(1, int(item.get("keep", 1)))
            except Exception:
                continue
            if result in (1, 2):
                seq.extend([result] * keep)
        return seq[-500:]

    @staticmethod
    def smoothed_ratio(count: int, total: int, alpha: float = 1.0) -> float:
        return (count + alpha) / (total + alpha * 2) if total >= 0 else 0.5

    def analyze_sequence(self, ptype: str, seq: List[int], target_round: int, source: str) -> Prediction:
        meta = ITEMS[ptype]
        labels = meta["labels"]
        name = str(meta["name"])

        if len(seq) < 8:
            return Prediction(ptype, name, target_round, 1, labels[1], 50.0, "데이터부족", "데이터 부족", source)  # type: ignore[index]

        counts_all = Counter(seq)
        last = seq[-1]
        prev = seq[-2] if len(seq) >= 2 else last
        opposite = 1 if last == 2 else 2
        windows = {12: seq[-12:], 20: seq[-20:], 50: seq[-50:], 100: seq[-100:], 200: seq[-200:]}

        t1 = defaultdict(Counter)
        t2 = defaultdict(Counter)
        t3 = defaultdict(Counter)
        for a, b in zip(seq[:-1], seq[1:]):
            t1[a][b] += 1
        for a, b, c in zip(seq[:-2], seq[1:-1], seq[2:]):
            t2[(a, b)][c] += 1
        for a, b, c, d in zip(seq[:-3], seq[1:-2], seq[2:-1], seq[3:]):
            t3[(a, b, c)][d] += 1

        streak = 1
        for value in reversed(seq[:-1]):
            if value == last:
                streak += 1
            else:
                break

        run_lengths = defaultdict(list)
        i = 0
        while i < len(seq):
            j = i + 1
            while j < len(seq) and seq[j] == seq[i]:
                j += 1
            run_lengths[seq[i]].append(j - i)
            i = j
        same_runs = run_lengths[last] or [2]
        avg_streak = sum(same_runs) / len(same_runs)
        longer_or_equal = sum(1 for n in same_runs if n >= streak)
        continuation_rate = self.smoothed_ratio(longer_or_equal, len(same_runs))

        tail = windows[12]
        alternation_rate = sum(1 for a, b in zip(tail[:-1], tail[1:]) if a != b) / max(1, len(tail) - 1)

        scores: Dict[int, float] = {}
        details: Dict[int, str] = {}
        for candidate in (1, 2):
            overall = self.smoothed_ratio(counts_all[candidate], len(seq))
            recent = (
                0.38 * self.smoothed_ratio(Counter(windows[12])[candidate], len(windows[12]))
                + 0.28 * self.smoothed_ratio(Counter(windows[20])[candidate], len(windows[20]))
                + 0.18 * self.smoothed_ratio(Counter(windows[50])[candidate], len(windows[50]))
                + 0.10 * self.smoothed_ratio(Counter(windows[100])[candidate], len(windows[100]))
                + 0.06 * self.smoothed_ratio(Counter(windows[200])[candidate], len(windows[200]))
            )
            trans1 = self.smoothed_ratio(t1[last][candidate], sum(t1[last].values()))
            trans2 = self.smoothed_ratio(t2[(prev, last)][candidate], sum(t2[(prev, last)].values()))
            key3 = tuple(seq[-3:]) if len(seq) >= 3 else (prev, last, last)
            trans3 = self.smoothed_ratio(t3[key3][candidate], sum(t3[key3].values()))
            transition = 0.45 * trans1 + 0.35 * trans2 + 0.20 * trans3

            streak_score = continuation_rate if candidate == last else 1.0 - continuation_rate
            if streak >= max(2, round(avg_streak)):
                streak_score = 0.65 * streak_score + 0.35 * (1.0 if candidate == opposite else 0.0)
            alternation_score = alternation_rate if candidate == opposite else (1.0 - alternation_rate)

            raw = 0.16 * overall + 0.36 * recent + 0.27 * transition + 0.13 * streak_score + 0.08 * alternation_score
            scores[candidate] = max(0.01, min(0.99, raw))
            details[candidate] = (
                f"전체 {overall*100:.1f}% / 최근 {recent*100:.1f}% / 전이 {transition*100:.1f}% / "
                f"연속 {streak}줄·평균 {avg_streak:.1f} / 교대 {alternation_rate*100:.1f}%"
            )

        total = scores[1] + scores[2]
        norm = {k: scores[k] / total for k in (1, 2)}
        pick_code = 1 if norm[1] >= norm[2] else 2
        edge = abs(norm[1] - norm[2])
        confidence = 50.0 + min(42.0, edge * 84.0)
        grade = "강" if confidence >= 67 else "중" if confidence >= 58 else "위험"
        return Prediction(ptype, name, target_round, pick_code, labels[pick_code], confidence, grade, details[pick_code], source)  # type: ignore[index]

    def build_predictions(self, current_round: int, table_seq: Dict[str, List[int]], pattern_data: Dict[str, dict]) -> Dict[str, Prediction]:
        predictions: Dict[str, Prediction] = {}
        for ptype in ("fd1", "fd2", "fd3", "fd4"):
            api_seq = self.expand_pattern_sequence(pattern_data.get(ptype, {}))
            html_seq = table_seq.get(ptype, [])
            if html_seq and api_seq:
                seq = api_seq[-240:] + html_seq[-40:] + html_seq[-20:]
                source = f"HTML {len(html_seq)}행 + API {len(api_seq)}"
            elif html_seq:
                seq = html_seq
                source = f"HTML {len(html_seq)}행"
            else:
                seq = api_seq
                source = f"API {len(api_seq)}"
            predictions[ptype] = self.analyze_sequence(ptype, seq, current_round, source)
        return predictions

    @staticmethod
    def actual_codes_from_row(row: ResultRow) -> Dict[str, int]:
        pb = row.powerball
        total = row.normal_sum
        return {
            "fd1": 1 if pb % 2 == 1 else 2,
            "fd2": 1 if pb <= 4 else 2,
            "fd3": 1 if total % 2 == 1 else 2,
            "fd4": 1 if total <= NORMAL_UNDER_MAX else 2,
        }

    def check_verdict(self, rows: List[ResultRow], pattern_data: Dict[str, dict]) -> Optional[str]:
        if not self.pending_predictions:
            return None
        target_round = min(p.target_round for p in self.pending_predictions.values())
        if target_round in self.notified_rounds:
            # 이미 이 회차는 판정 알림을 띄웠으므로 중복 알림을 차단합니다.
            self.pending_predictions = {}
            return None
        actual_codes: Dict[str, int] = {}
        for row in rows:
            if row.round_no == target_round:
                actual_codes = self.actual_codes_from_row(row)
                break

        if not actual_codes:
            try:
                settled_round = int(pattern_data.get("fd1", {}).get("update", {}).get("round") or 0)
            except Exception:
                settled_round = 0
            if settled_round < target_round:
                return None
            for ptype in ("fd1", "fd2", "fd3", "fd4"):
                try:
                    actual_codes[ptype] = int(pattern_data.get(ptype, {}).get("update", {}).get(ptype))
                except Exception:
                    pass

        if not actual_codes:
            return None

        hit_count = 0
        lines = [f"{target_round}회차 판정 결과", ""]
        csv_rows = []
        for ptype in ("fd1", "fd2", "fd3", "fd4"):
            pred = self.pending_predictions.get(ptype)
            actual = actual_codes.get(ptype)
            if pred is None or actual not in (1, 2):
                continue
            actual_label = ITEMS[ptype]["labels"][actual]  # type: ignore[index]
            hit = actual == pred.pick_code
            if hit:
                hit_count += 1
            mark = "적중!" if hit else "미적중!"
            lines.append(f"{pred.name}: 픽 {pred.pick_label} → 실제 {actual_label} = {mark}")
            csv_rows.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), target_round, pred.name, pred.pick_label, actual_label, mark, f"{pred.confidence:.2f}%"])

        if not csv_rows:
            return None
        lines.insert(1, f"총 {hit_count}/{len(csv_rows)} 적중")
        self.append_csv(csv_rows)
        self.notified_rounds.add(target_round)
        self.pending_predictions = {}
        return "\n".join(lines)

    def append_csv(self, rows: List[List[str]]) -> None:
        exists = os.path.exists(self.history_csv)
        with open(self.history_csv, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            if not exists:
                writer.writerow(["시간", "회차", "항목", "픽", "실제", "결과", "분석점수"])
            writer.writerows(rows)

    def analyze_once(self, force_new_pick: bool = False) -> Tuple[dict, Dict[str, Prediction], Optional[str], str, int, int]:
        live = self.fetch_live()
        time_set = live.get("time_set", {}) if isinstance(live, dict) else {}
        current_round = int(time_set.get("round") or self.estimate_round())
        next_time = int(time_set.get("nextTime") or 0)
        settled_round = max(1, current_round - 1)
        base_round = max(1, current_round - 2)

        table_seq, rows, _source, table_log = self.fetch_table_sequences()
        pattern_data: Dict[str, dict] = {}
        for ptype in ("fd1", "fd2", "fd3", "fd4"):
            try:
                pattern_data[ptype] = self.fetch_pattern(ptype, base_round)
            except Exception:
                pattern_data[ptype] = {}

        predictions = self.build_predictions(current_round, table_seq, pattern_data)
        verdict = self.check_verdict(rows, pattern_data)

        if current_round in self.notified_rounds:
            table_log += f"\n{current_round}회차는 이미 판정완료 - 중복 알림 차단"
        elif force_new_pick or current_round != self.last_analyzed_round or not self.pending_predictions:
            self.last_analyzed_round = current_round
            self.pending_predictions = predictions
            table_log += f"\n{current_round}회차 4항목 분석완료 100% / 픽 생성 완료"

        state = {
            "current_round": current_round,
            "settled_round": settled_round,
            "next_time": next_time,
            "rows_count": len(rows),
            "latest_row": max(rows, key=lambda r: (r.date, r.round_no)).round_no if rows else 0,
        }
        return state, predictions, verdict, table_log, current_round, next_time


class PredictionCard(BoxLayout):
    def __init__(self, **kwargs) -> None:
        super().__init__(orientation="vertical", padding=dp(8), spacing=dp(3), size_hint_y=None, height=dp(95), **kwargs)
        self.title = Label(text="-", markup=True, size_hint_y=None, height=dp(25), halign="left", valign="middle")
        self.pick = Label(text="-", markup=True, size_hint_y=None, height=dp(28), halign="left", valign="middle")
        self.detail = Label(text="-", markup=True, font_size="12sp", halign="left", valign="top")
        for w in (self.title, self.pick, self.detail):
            w.bind(size=lambda inst, val: setattr(inst, "text_size", (inst.width, None)))
            self.add_widget(w)

    def set_prediction(self, pred: Prediction) -> None:
        color = "ffcc00" if pred.grade == "강" else "44dd88" if pred.grade == "중" else "ff7777"
        self.title.text = f"[b]{pred.name}[/b]  [color=44dd88]분석완료 100%[/color]"
        self.pick.text = f"[size=20sp][b]{pred.target_round}회차-{pred.pick_label}[/b][/size]  [color={color}]{pred.grade}[/color]"
        self.detail.text = f"[size=11sp]{pred.source} / 내부점수 {pred.confidence:.2f}%\n{pred.detail}[/size]"


class MainView(BoxLayout):
    running = BooleanProperty(True)
    status_text = StringProperty("준비 중")

    def __init__(self, analyzer: DhrAnalyzer, **kwargs) -> None:
        super().__init__(orientation="vertical", padding=dp(10), spacing=dp(8), **kwargs)
        self.analyzer = analyzer
        self.fetching = False
        self.cards: Dict[str, PredictionCard] = {}
        self.last_log = ""
        self.next_refresh_sec = 5
        # UI 레벨에서도 판정 팝업 중복을 한 번 더 막습니다.
        self.shown_verdict_keys: set[str] = set()
        self._build_ui()

    def _build_ui(self) -> None:
        Window.clearcolor = (0.03, 0.06, 0.11, 1)

        title = Label(
            text="[b]동행파워볼 APK 자동픽 v3[/b]\n[size=12sp]4항목 분석완료 100% 표시 + 회차별 알림 1회[/size]",
            markup=True,
            size_hint_y=None,
            height=dp(58),
            halign="left",
            valign="middle",
        )
        title.bind(size=lambda inst, val: setattr(inst, "text_size", (inst.width, None)))
        self.add_widget(title)

        self.round_label = Label(text="현재 회차: -- / 남은 시간: --:--", markup=True, size_hint_y=None, height=dp(34), halign="left")
        self.round_label.bind(size=lambda inst, val: setattr(inst, "text_size", (inst.width, None)))
        self.add_widget(self.round_label)

        btns = GridLayout(cols=3, spacing=dp(6), size_hint_y=None, height=dp(46))
        btns.add_widget(Button(text="즉시 분석", on_release=lambda *_: self.refresh(force=True)))
        btns.add_widget(Button(text="자동 ON/OFF", on_release=lambda *_: self.toggle_running()))
        btns.add_widget(Button(text="사이트 열기", on_release=lambda *_: self.open_site()))
        self.add_widget(btns)

        self.status_label = Label(text="상태: 준비 완료", markup=True, size_hint_y=None, height=dp(40), halign="left", valign="middle")
        self.status_label.bind(size=lambda inst, val: setattr(inst, "text_size", (inst.width, None)))
        self.add_widget(self.status_label)

        card_scroll = ScrollView(size_hint_y=0.47)
        card_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        card_box.bind(minimum_height=card_box.setter("height"))
        for ptype in ("fd1", "fd2", "fd3", "fd4"):
            card = PredictionCard()
            self.cards[ptype] = card
            card_box.add_widget(card)
        card_scroll.add_widget(card_box)
        self.add_widget(card_scroll)

        self.log_label = Label(text="[b]로그[/b]\n", markup=True, size_hint_y=None, halign="left", valign="top")
        self.log_label.bind(texture_size=lambda inst, val: setattr(inst, "height", max(dp(180), inst.texture_size[1] + dp(20))))
        self.log_label.bind(size=lambda inst, val: setattr(inst, "text_size", (inst.width, None)))
        log_scroll = ScrollView(size_hint_y=0.35)
        log_scroll.add_widget(self.log_label)
        self.add_widget(log_scroll)

    def start(self) -> None:
        self.add_log("앱 시작 - Android Kivy 안정형")
        self.refresh(force=True)
        Clock.schedule_interval(self._tick, 1)

    def _tick(self, dt: float) -> None:
        if not self.running or self.fetching:
            return
        self.next_refresh_sec -= 1
        if self.next_refresh_sec <= 0:
            self.refresh(force=False)

    def toggle_running(self) -> None:
        self.running = not self.running
        self.add_log("자동분석: ON" if self.running else "자동분석: OFF")
        self.status_label.text = f"상태: {'자동분석 ON' if self.running else '자동분석 OFF'}"
        if self.running:
            self.refresh(force=True)

    def open_site(self) -> None:
        try:
            if platform == "android":
                from jnius import autoclass
                Intent = autoclass("android.content.Intent")
                Uri = autoclass("android.net.Uri")
                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                intent = Intent(Intent.ACTION_VIEW, Uri.parse(GAME_URL))
                PythonActivity.mActivity.startActivity(intent)
            else:
                webbrowser.open(GAME_URL)
            self.add_log("사이트 열기 실행")
        except Exception as exc:
            self.popup("사이트 열기 실패", str(exc))

    def refresh(self, force: bool = False) -> None:
        if self.fetching:
            return
        self.fetching = True
        self.status_label.text = "상태: 분석 중..."
        threading.Thread(target=self._worker, args=(force,), daemon=True).start()

    def _worker(self, force: bool) -> None:
        try:
            state, predictions, verdict, log_msg, _current, next_time = self.analyzer.analyze_once(force_new_pick=force)
            # Android 배터리/네트워크 절약: 결과 시간이 가까울 때만 빠르게 갱신합니다.
            if next_time <= 12:
                self.next_refresh_sec = 2
            elif next_time <= 35:
                self.next_refresh_sec = 4
            elif next_time <= 90:
                self.next_refresh_sec = 10
            else:
                self.next_refresh_sec = 20
            Clock.schedule_once(lambda dt: self.apply_result(state, predictions, verdict, log_msg), 0)
        except Exception as exc:
            Clock.schedule_once(lambda dt: self.apply_error(exc), 0)

    def apply_result(self, state: dict, predictions: Dict[str, Prediction], verdict: Optional[str], log_msg: str) -> None:
        self.fetching = False
        m, s = divmod(max(0, int(state.get("next_time", 0))), 60)
        self.round_label.text = (
            f"[b]현재 {state.get('current_round', '--')}회차[/b] / "
            f"최근 확정 {state.get('settled_round', '--')}회차 / 남은 시간 {m:02d}:{s:02d}"
        )
        self.status_label.text = f"상태: 결과표 {state.get('rows_count', 0)}행 / 다음 갱신 {self.next_refresh_sec}초"
        for ptype, pred in predictions.items():
            if ptype in self.cards:
                self.cards[ptype].set_prediction(pred)
        if log_msg and log_msg != self.last_log:
            self.last_log = log_msg
            for line in log_msg.split("\n"):
                self.add_log(line)
        if verdict:
            verdict_key = verdict.split("\n", 1)[0].strip()
            if verdict_key not in self.shown_verdict_keys:
                self.shown_verdict_keys.add(verdict_key)
                self.add_log(verdict.replace("\n", " | "))
                title = "4개 항목 모두 적중!" if "총 4/4" in verdict else "전체 미적중" if "총 0/" in verdict else "부분 적중"
                self.popup(title, verdict)

    def apply_error(self, exc: Exception) -> None:
        self.fetching = False
        self.next_refresh_sec = 8
        msg = str(exc)[:220]
        self.status_label.text = f"상태: 오류 - {msg}"
        self.add_log(f"오류: {msg}")

    def add_log(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        safe = str(message).replace("[", "〔").replace("]", "〕")
        old = self.log_label.text
        lines = old.split("\n")[-70:]
        lines.append(f"[{ts}] {safe}")
        self.log_label.text = "\n".join(lines)

    def popup(self, title: str, message: str) -> None:
        content = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(10))
        label = Label(text=message, halign="left", valign="top")
        label.bind(size=lambda inst, val: setattr(inst, "text_size", (inst.width, None)))
        content.add_widget(label)
        close_btn = Button(text="확인", size_hint_y=None, height=dp(44))
        content.add_widget(close_btn)
        pop = Popup(title=title, content=content, size_hint=(0.9, 0.55))
        close_btn.bind(on_release=pop.dismiss)
        pop.open()


class DhrPowerballApkApp(App):
    def build(self):
        self.title = APP_TITLE
        self.analyzer = DhrAnalyzer(self.user_data_dir)
        self.view = MainView(self.analyzer)
        return self.view

    def on_start(self) -> None:
        self.view.start()

    def on_stop(self) -> None:
        try:
            self.view.running = False
        except Exception:
            pass


if __name__ == "__main__":
    DhrPowerballApkApp().run()
