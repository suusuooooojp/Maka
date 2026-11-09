"""Simple, Easy-to-Play Online 4-Player Trump (Lobby + 1 Room per Rule: Daifugo & Babanuki + Spectator Mode) for School Hub."""
from __future__ import annotations

import asyncio
import random
import time
import uuid
from typing import Dict, List, Optional, Set

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

SERVICE_NAME = "トランプ (大富豪・ババ抜き)"
SERVICE_ICON = "🃏"
SERVICE_PATH = "/trump"

# ============================================================================
# Card & Rule Definitions (Server-Authoritative Engine + Strong AI)
# ============================================================================

SUITS = [
    {"code": "S", "symbol": "♠", "color": "black", "name": "スペード"},
    {"code": "H", "symbol": "♥", "color": "red", "name": "ハート"},
    {"code": "D", "symbol": "♦", "color": "red", "name": "ダイヤ"},
    {"code": "C", "symbol": "♣", "color": "black", "name": "クラブ"},
]

DAIFUGO_RANKS = [
    {"num": 3, "label": "3", "power": 1},
    {"num": 4, "label": "4", "power": 2},
    {"num": 5, "label": "5", "power": 3},
    {"num": 6, "label": "6", "power": 4},
    {"num": 7, "label": "7", "power": 5},
    {"num": 8, "label": "8", "power": 6},
    {"num": 9, "label": "9", "power": 7},
    {"num": 10, "label": "10", "power": 8},
    {"num": 11, "label": "J", "power": 9},
    {"num": 12, "label": "Q", "power": 10},
    {"num": 13, "label": "K", "power": 11},
    {"num": 1, "label": "A", "power": 12},
    {"num": 2, "label": "2", "power": 13},
]

BOT_PROFILES = [
    {"name": "レン (AI)", "style": "Master", "avatar": "R"},
    {"name": "カイ (AI)", "style": "Power", "avatar": "K"},
    {"name": "レイ (AI)", "style": "Tactics", "avatar": "L"},
    {"name": "ミナ (AI)", "style": "Brain", "avatar": "M"},
]

RANK_TITLES_DAIFUGO = ["大富豪", "富豪", "貧民", "大貧民"]
RANK_TITLES_BABANUKI = ["1位抜け", "2位抜け", "3位抜け", "ババ持ち"]


def _build_deck(mode: str) -> List[dict]:
    deck: List[dict] = []
    uid = 1
    for s in SUITS:
        for r in DAIFUGO_RANKS:
            deck.append({
                "id": f"c_{uid}",
                "suit": s["code"],
                "suitSymbol": s["symbol"],
                "color": s["color"],
                "num": r["num"],
                "label": r["label"],
                "basePower": r["power"],
                "isJoker": False,
            })
            uid += 1
    joker_count = 2 if mode == "daifugo" else 1
    for j in range(joker_count):
        deck.append({
            "id": f"joker_{j + 1}",
            "suit": "JOKER",
            "suitSymbol": "🃏",
            "color": "joker",
            "num": 99,
            "label": "JOKER",
            "basePower": 99,
            "isJoker": True,
        })
    random.shuffle(deck)
    return deck


class TrumpRoom:
    """Single shared online room for a specific game mode ('daifugo' or 'babanuki')."""

    def __init__(self, mode: str, title: str, description: str) -> None:
        self.mode = mode  # 'daifugo' | 'babanuki'
        self.title = title
        self.description = description
        self.lock = asyncio.Lock()
        self.clients: Dict[str, WebSocket] = {}
        self.participants: Dict[str, dict] = {}  # pid -> {id, name, last_seen, prefer_spectate}
        self.seats: List[Optional[str]] = [None, None, None, None]  # pid or None (AI Bot)
        self.seat_ranks: List[str] = ["平民", "平民", "平民", "平民"]
        self.hands: List[List[dict]] = [[], [], [], []]
        self.round_number: int = 1
        self.phase: str = "playing"  # 'playing' | 'round_end'
        self.phase_since: float = time.time()
        self.current_turn: int = 0
        self.turn_since: float = time.time()
        self.finish_order: List[int] = []
        self.miyako_seat: Optional[int] = None
        self.passed_seats: Set[int] = set()
        self.table_play: Optional[dict] = None
        self.prev_table_play: Optional[dict] = None
        self.last_play_seat: Optional[int] = None
        self.is_revolution: bool = False
        self.is_eleven_back: bool = False
        self.locked_suits: Optional[List[str]] = None
        self.rules: Dict[str, bool] = {
            "revolution": True,
            "eightGiri": True,
            "elevenBack": True,
            "shibari": True,
            "spade3": True,
            "stairs": True,
            "miyakoOchi": True,
        }
        self.event_seq: int = 0
        self.recent_events: List[dict] = []
        self._start_new_round(reset_ranks=True)

    def _emit_event(self, kind: str, text: str = "", seat: int = -1, sfx: str = "") -> None:
        self.event_seq += 1
        self.recent_events.append({
            "seq": self.event_seq,
            "ts": time.time(),
            "kind": kind,
            "text": text,
            "seat": seat,
            "sfx": sfx,
        })
        if len(self.recent_events) > 25:
            self.recent_events = self.recent_events[-25:]

    def is_reversed(self) -> bool:
        return bool(self.is_revolution) ^ bool(self.is_eleven_back)

    def card_power(self, card: dict) -> int:
        if card.get("isJoker"):
            return 99
        bp = int(card.get("basePower") or 1)
        return (14 - bp) if self.is_reversed() else bp

    def sort_hand(self, seat: int) -> None:
        h = self.hands[seat]
        if self.mode == "babanuki":
            h.sort(key=lambda c: (1 if c.get("isJoker") else 0, int(c.get("num") or 0), str(c.get("suit") or "")))
        else:
            h.sort(key=lambda c: (self.card_power(c), str(c.get("suit") or "")))

    def seat_display_name(self, seat: int) -> str:
        pid = self.seats[seat]
        if pid and pid in self.participants:
            return str(self.participants[pid].get("name") or f"プレイヤー{seat + 1}")
        return BOT_PROFILES[seat]["name"]

    def is_seat_human(self, seat: int) -> bool:
        pid = self.seats[seat]
        return bool(pid and pid in self.participants)

    def remove_participant(self, pid: str) -> None:
        self.participants.pop(pid, None)
        for s in range(4):
            if self.seats[s] == pid:
                self.seats[s] = None

    def touch_participant(self, pid: str, name: Optional[str] = None, prefer_spectate: Optional[bool] = None) -> None:
        now = time.time()
        p = self.participants.get(pid)
        if p is None:
            clean_name = (name or "").strip()[:16] or f"ゲスト{random.randint(100, 999)}"
            p = {
                "id": pid,
                "name": clean_name,
                "last_seen": now,
                "prefer_spectate": bool(prefer_spectate) if prefer_spectate is not None else False,
            }
            self.participants[pid] = p
        else:
            p["last_seen"] = now
            if name and name.strip():
                p["name"] = name.strip()[:16]
            if prefer_spectate is not None:
                p["prefer_spectate"] = bool(prefer_spectate)

        if p["prefer_spectate"]:
            for s in range(4):
                if self.seats[s] == pid:
                    self.seats[s] = None
            return

        if pid in self.seats:
            return

        for s in range(4):
            if self.seats[s] is None or self.seats[s] not in self.participants:
                self.seats[s] = pid
                self._emit_event("bubble", f"{p['name']}さんが着席", seat=s, sfx="select")
                return

    def prune_stale_participants(self) -> bool:
        now = time.time()
        changed = False
        stale_pids = [
            pid for pid, p in self.participants.items()
            if pid not in self.clients and (now - float(p.get("last_seen") or 0)) > 22.0
        ]
        for pid in stale_pids:
            self.remove_participant(pid)
            changed = True

        for pid, p in list(self.participants.items()):
            if not p.get("prefer_spectate") and pid not in self.seats:
                for s in range(4):
                    if self.seats[s] is None:
                        self.seats[s] = pid
                        changed = True
                        break
        return changed

    def get_summary(self) -> dict:
        self.prune_stale_participants()
        human_seats = sum(1 for s in range(4) if self.is_seat_human(s))
        spec_count = sum(1 for k in self.participants if k not in self.seats)
        seated_names = [self.seat_display_name(s) for s in range(4)]
        return {
            "mode": self.mode,
            "title": self.title,
            "description": self.description,
            "humanCount": human_seats,
            "maxSeats": 4,
            "spectatorCount": spec_count,
            "isFull": human_seats >= 4,
            "roundNumber": self.round_number,
            "seatedNames": seated_names,
        }

    # ------------------------------------------------------------------------
    # Round Initialization & Card Exchange
    # ------------------------------------------------------------------------
    def _start_new_round(self, reset_ranks: bool = False) -> None:
        if reset_ranks:
            self.round_number = 1
            self.seat_ranks = ["平民", "平民", "平民", "平民"]

        self.phase = "playing"
        self.phase_since = time.time()
        self.is_revolution = False
        self.is_eleven_back = False
        self.locked_suits = None
        self.table_play = None
        self.prev_table_play = None
        self.last_play_seat = None
        self.passed_seats.clear()
        self.finish_order.clear()
        self.miyako_seat = None

        deck = _build_deck(self.mode)
        self.hands = [[], [], [], []]
        for idx, card in enumerate(deck):
            self.hands[idx % 4].append(card)

        if self.mode == "daifugo":
            for s in range(4):
                self.sort_hand(s)
            if not reset_ranks and self.round_number >= 2:
                self._perform_card_exchange()
            starter = 0
            for s in range(4):
                if any(c.get("suit") == "D" and c.get("num") == 3 for c in self.hands[s]):
                    starter = s
                    break
            self.current_turn = starter
        else:
            for s in range(4):
                self._discard_babanuki_pairs(s, emit=False)
                random.shuffle(self.hands[s])
            self.current_turn = 0

        self.turn_since = time.time()
        self._emit_event("sfx", "", seat=-1, sfx="card")

    def _perform_card_exchange(self) -> None:
        try:
            daifugo = self.seat_ranks.index("大富豪")
            daihinmin = self.seat_ranks.index("大貧民")
            self._swap_cards(daifugo, daihinmin, 2)
        except ValueError:
            pass
        try:
            fugo = self.seat_ranks.index("富豪")
            hinmin = self.seat_ranks.index("貧民")
            self._swap_cards(fugo, hinmin, 1)
        except ValueError:
            pass
        for s in range(4):
            self.sort_hand(s)
        self._emit_event("cutin", "階級カード交換完了", sfx="card")

    def _swap_cards(self, high_s: int, low_s: int, count: int) -> None:
        self.sort_hand(high_s)
        self.sort_hand(low_s)
        best_from_low = self.hands[low_s][-count:]
        self.hands[low_s] = self.hands[low_s][:-count]
        give_from_high: List[dict] = []
        i = 0
        while i < len(self.hands[high_s]) and len(give_from_high) < count:
            c = self.hands[high_s][i]
            if not c.get("isJoker") and c.get("num") != 8:
                give_from_high.append(self.hands[high_s].pop(i))
            else:
                i += 1
        while len(give_from_high) < count and self.hands[high_s]:
            give_from_high.append(self.hands[high_s].pop(0))
        self.hands[high_s].extend(best_from_low)
        self.hands[low_s].extend(give_from_high)

    # ------------------------------------------------------------------------
    # Daifugo Move Analysis & Legality
    # ------------------------------------------------------------------------
    def analyze_combo(self, cards: List[dict]) -> Optional[dict]:
        if not cards:
            return None
        n = len(cards)
        jokers = [c for c in cards if c.get("isJoker")]
        normals = [c for c in cards if not c.get("isJoker")]

        if n == 1:
            c = cards[0]
            return {
                "type": "single",
                "count": 1,
                "power": self.card_power(c),
                "basePower": int(c.get("basePower") or 1),
                "suits": ["JOKER"] if c.get("isJoker") else [str(c.get("suit"))],
                "hasEight": (not c.get("isJoker")) and c.get("num") == 8,
                "hasEleven": (not c.get("isJoker")) and c.get("num") == 11,
                "isSpade3": (not c.get("isJoker")) and c.get("suit") == "S" and c.get("num") == 3,
                "isSingleJoker": bool(c.get("isJoker")),
                "cards": list(cards),
            }

        if not normals:
            return {
                "type": "group",
                "count": n,
                "power": 99,
                "basePower": 99,
                "suits": [str(c.get("suit")) for c in cards],
                "hasEight": False,
                "hasEleven": False,
                "isSpade3": False,
                "isSingleJoker": False,
                "cards": list(cards),
            }

        first_num = normals[0].get("num")
        if all(c.get("num") == first_num for c in normals) and n <= 5:
            return {
                "type": "group",
                "count": n,
                "power": self.card_power(normals[0]),
                "basePower": int(normals[0].get("basePower") or 1),
                "suits": [str(c.get("suit")) for c in normals],
                "hasEight": first_num == 8,
                "hasEleven": first_num == 11,
                "isSpade3": False,
                "isSingleJoker": False,
                "cards": list(cards),
            }

        if self.rules.get("stairs", True) and n >= 3 and normals:
            suit0 = normals[0].get("suit")
            if all(c.get("suit") == suit0 for c in normals):
                s_norm = sorted(normals, key=lambda c: int(c.get("basePower") or 0))
                needed = 0
                dup = False
                for i in range(1, len(s_norm)):
                    diff = int(s_norm[i]["basePower"]) - int(s_norm[i - 1]["basePower"])
                    if diff == 0:
                        dup = True
                        break
                    needed += diff - 1
                if not dup and needed <= len(jokers):
                    powers = [self.card_power(c) for c in normals]
                    anchor = min(powers) if self.is_reversed() else max(powers)
                    return {
                        "type": "stairs",
                        "count": n,
                        "power": anchor,
                        "basePower": int(s_norm[-1]["basePower"]),
                        "suits": [str(suit0)],
                        "hasEight": any(c.get("num") == 8 for c in normals),
                        "hasEleven": any(c.get("num") == 11 for c in normals),
                        "isSpade3": False,
                        "isSingleJoker": False,
                        "cards": list(cards),
                    }
        return None

    def _suits_match_lock(self, combo: dict) -> bool:
        if not self.locked_suits:
            return True
        if combo.get("isSingleJoker"):
            return True
        if combo.get("type") == "stairs":
            return combo["suits"][0] in self.locked_suits
        jokers_cnt = sum(1 for c in combo["cards"] if c.get("isJoker"))
        c_suits = [str(c.get("suit")) for c in combo["cards"] if not c.get("isJoker")]
        missing = 0
        for ls in self.locked_suits:
            if ls in c_suits:
                c_suits.remove(ls)
            else:
                missing += 1
        return missing <= jokers_cnt

    def is_legal_play(self, combo: Optional[dict]) -> bool:
        if not combo:
            return False
        if not self.table_play:
            return True
        if self.rules.get("spade3", True) and self.table_play.get("isSingleJoker") and combo.get("isSpade3"):
            return True
        if combo["type"] != self.table_play["type"] or combo["count"] != self.table_play["count"]:
            return False
        if self.rules.get("shibari", True) and self.locked_suits and not self._suits_match_lock(combo):
            return False
        return int(combo["power"]) > int(self.table_play["power"])

    def generate_legal_combos(self, seat: int) -> List[dict]:
        hand = self.hands[seat]
        results: List[dict] = []
        jokers = [c for c in hand if c.get("isJoker")]
        normals = [c for c in hand if not c.get("isJoker")]

        for c in hand:
            cb = self.analyze_combo([c])
            if self.is_legal_play(cb):
                results.append(cb)

        by_num: Dict[int, List[dict]] = {}
        for c in normals:
            by_num.setdefault(int(c["num"]), []).append(c)
        for _, grp in by_num.items():
            for sz in range(2, len(grp) + 1):
                sub = grp[:sz]
                cb = self.analyze_combo(sub)
                if self.is_legal_play(cb):
                    results.append(cb)
                if jokers and sz + 1 <= 4:
                    cb_j = self.analyze_combo(sub + [jokers[0]])
                    if self.is_legal_play(cb_j):
                        results.append(cb_j)
            if len(grp) == 1 and jokers:
                cb_p = self.analyze_combo([grp[0], jokers[0]])
                if self.is_legal_play(cb_p):
                    results.append(cb_p)

        if self.rules.get("stairs", True):
            for s in SUITS:
                sc = sorted(
                    [c for c in normals if c.get("suit") == s["code"]],
                    key=lambda x: int(x["basePower"]),
                )
                for i in range(len(sc)):
                    seq = [sc[i]]
                    for j in range(i + 1, len(sc)):
                        if int(sc[j]["basePower"]) == int(seq[-1]["basePower"]) + 1:
                            seq.append(sc[j])
                            if len(seq) >= 3:
                                cb_st = self.analyze_combo(list(seq))
                                if self.is_legal_play(cb_st):
                                    results.append(cb_st)
                        else:
                            break
        return results

    def choose_best_ai_move(self, seat: int) -> Optional[dict]:
        hand = self.hands[seat]
        legal = self.generate_legal_combos(seat)
        if not legal:
            return None

        opp_counts = [
            len(self.hands[p])
            for p in range(4)
            if p != seat and p not in self.finish_order and len(self.hands[p]) > 0
        ]
        min_opp = min(opp_counts) if opp_counts else 99
        opp_near_win = min_opp <= 3

        freq: Dict[int, int] = {}
        for c in hand:
            if not c.get("isJoker"):
                freq[int(c["num"])] = freq.get(int(c["num"]), 0) + 1

        avg_power = sum(self.card_power(c) for c in hand) / max(1, len(hand))

        best_move = None
        best_score = -1e9
        for mv in legal:
            rem = len(hand) - mv["count"]
            if rem == 0:
                return mv
            score = 0.0
            if not self.table_play:
                score += mv["count"] * 26.0
                score -= mv["power"] * 3.8
                if mv["hasEight"] and rem <= 3:
                    score += 90.0
                elif mv["hasEight"] and rem > 5:
                    score -= 28.0
                if mv["type"] == "single" and not mv["isSingleJoker"]:
                    f = freq.get(int(mv["cards"][0]["num"]), 1)
                    if f >= 2:
                        score -= 34.0 * f
                if mv["isSingleJoker"] and rem > 2:
                    score -= 120.0
            else:
                if opp_near_win:
                    score += mv["power"] * 3.2
                    if mv["hasEight"]:
                        score += 85.0
                    if mv["hasEleven"]:
                        score += 45.0
                    if self.rules.get("shibari", True) and self.table_play.get("suits"):
                        if any(s in self.table_play["suits"] for s in mv["suits"]):
                            score += 42.0
                else:
                    score -= mv["power"] * 3.4
                    score += mv["count"] * 18.0
                    if mv["type"] == "single" and not mv["isSingleJoker"]:
                        f = freq.get(int(mv["cards"][0]["num"]), 1)
                        if f >= 2:
                            score -= 28.0 * f
                    if any(c.get("isJoker") for c in mv["cards"]) and rem > 4:
                        score -= 65.0
                    if self.table_play.get("isSingleJoker") and mv.get("isSpade3"):
                        score += 500.0
                    if mv["hasEight"]:
                        score += 60.0 if rem <= 5 else 12.0
                    if mv["hasEleven"] and avg_power < 7.0:
                        score += 35.0

            if self.rules.get("revolution", True) and mv["count"] >= 4 and mv["type"] == "group":
                score += 120.0 if avg_power <= 6.5 else -90.0

            if score > best_score:
                best_score = score
                best_move = mv

        if self.table_play and not opp_near_win and len(hand) >= 7 and best_score < -42.0:
            return None
        return best_move

    # ------------------------------------------------------------------------
    # Daifugo Execution
    # ------------------------------------------------------------------------
    def execute_daifugo_play(self, seat: int, combo: dict) -> None:
        used_ids = {c["id"] for c in combo["cards"]}
        self.hands[seat] = [c for c in self.hands[seat] if c["id"] not in used_ids]

        triggered_shibari = False
        if (
            self.rules.get("shibari", True)
            and self.table_play
            and not self.locked_suits
            and not combo.get("isSingleJoker")
            and not self.table_play.get("isSingleJoker")
        ):
            prev_s = sorted(self.table_play.get("suits") or [])
            curr_s = sorted(combo.get("suits") or [])
            if prev_s and prev_s == curr_s:
                self.locked_suits = curr_s
                triggered_shibari = True

        triggered_spade3 = bool(
            self.rules.get("spade3", True)
            and self.table_play
            and self.table_play.get("isSingleJoker")
            and combo.get("isSpade3")
        )

        p_name = self.seat_display_name(seat)
        if combo["type"] == "stairs":
            desc = f"{combo['count']}枚階段"
        elif combo["count"] == 1:
            desc = combo["cards"][0]["label"]
        elif combo["count"] == 2:
            desc = f"{combo['cards'][0]['label']} ペア"
        elif combo["count"] == 3:
            desc = f"{combo['cards'][0]['label']} 3枚組"
        else:
            desc = f"{combo['count']}枚出し"

        self.prev_table_play = self.table_play
        self.table_play = {
            **combo,
            "seat": seat,
            "label": f"{p_name}: {desc}",
        }
        self.last_play_seat = seat
        self.passed_seats.clear()

        triggered_rev = False
        if self.rules.get("revolution", True) and (
            (combo["count"] >= 4 and combo["type"] == "group")
            or (combo["count"] >= 5 and combo["type"] == "stairs")
        ):
            self.is_revolution = not self.is_revolution
            triggered_rev = True
            for s in range(4):
                self.sort_hand(s)

        triggered_11 = False
        if self.rules.get("elevenBack", True) and combo.get("hasEleven") and not self.is_eleven_back:
            self.is_eleven_back = True
            triggered_11 = True
            for s in range(4):
                self.sort_hand(s)

        triggered_8 = bool(self.rules.get("eightGiri", True) and combo.get("hasEight"))

        if triggered_rev:
            self._emit_event("cutin", "革命発動！(強さ逆転)" if self.is_revolution else "革命返し！", seat=seat, sfx="special")
        elif triggered_spade3:
            self._emit_event("cutin", "♠3 スペ3返し！", seat=seat, sfx="special")
        elif triggered_8:
            self._emit_event("cutin", "8切り！場流し", seat=seat, sfx="special")
        elif triggered_11:
            self._emit_event("cutin", "11バック！", seat=seat, sfx="special")
        elif triggered_shibari:
            self._emit_event("cutin", "しばり発動！", seat=seat, sfx="special")
        else:
            self._emit_event("sfx", "", seat=seat, sfx="card")

        if len(self.hands[seat]) == 1:
            self._emit_event("bubble", "ラスト1枚！", seat=seat)
        elif len(self.hands[seat]) == 0 and seat not in self.finish_order:
            self.finish_order.append(seat)
            self._emit_event("bubble", "上がり！", seat=seat, sfx="special")
            if self.rules.get("miyakoOchi", True) and len(self.finish_order) == 1:
                if "大富豪" in self.seat_ranks:
                    prev_df = self.seat_ranks.index("大富豪")
                    if prev_df != seat and prev_df not in self.finish_order:
                        self._emit_event("cutin", f"{self.seat_display_name(prev_df)}が都落ち！", seat=prev_df, sfx="joker")
                        self.hands[prev_df] = []
                        self.miyako_seat = prev_df

        active_seats = [
            s for s in range(4)
            if len(self.hands[s]) > 0 and s not in self.finish_order and s != self.miyako_seat
        ]
        if len(active_seats) <= 1:
            for rem in active_seats:
                if rem not in self.finish_order:
                    self.finish_order.append(rem)
            if self.miyako_seat is not None and self.miyako_seat not in self.finish_order:
                self.finish_order.append(self.miyako_seat)
            self._conclude_round()
            return

        if triggered_8 or triggered_spade3:
            self._clear_trick()
            self.current_turn = seat if len(self.hands[seat]) > 0 else self._next_active_seat(seat)
            self.turn_since = time.time()
            return

        self._advance_daifugo_turn(seat)

    def execute_daifugo_pass(self, seat: int) -> None:
        self.passed_seats.add(seat)
        self._emit_event("bubble", "パス", seat=seat, sfx="pass")
        self._advance_daifugo_turn(seat)

    def _clear_trick(self) -> None:
        self.prev_table_play = None
        self.table_play = None
        self.is_eleven_back = False
        self.locked_suits = None
        self.passed_seats.clear()
        for s in range(4):
            self.sort_hand(s)

    def _next_active_seat(self, from_seat: int) -> int:
        for step in range(1, 5):
            cand = (from_seat + step) % 4
            if len(self.hands[cand]) > 0 and cand not in self.finish_order:
                return cand
        return 0

    def _advance_daifugo_turn(self, from_seat: int) -> None:
        active = [s for s in range(4) if len(self.hands[s]) > 0 and s not in self.finish_order]
        if len(active) <= 1:
            for rem in active:
                if rem not in self.finish_order:
                    self.finish_order.append(rem)
            self._conclude_round()
            return

        next_s: Optional[int] = None
        for step in range(1, 5):
            cand = (from_seat + step) % 4
            if cand == self.last_play_seat:
                self._clear_trick()
                next_s = cand if len(self.hands[cand]) > 0 else self._next_active_seat(cand)
                break
            if len(self.hands[cand]) > 0 and cand not in self.finish_order and cand not in self.passed_seats:
                next_s = cand
                break

        if next_s is None:
            self._clear_trick()
            next_s = self._next_active_seat(self.last_play_seat if self.last_play_seat is not None else from_seat)

        self.current_turn = next_s
        self.turn_since = time.time()

    # ------------------------------------------------------------------------
    # Babanuki Execution
    # ------------------------------------------------------------------------
    def _discard_babanuki_pairs(self, seat: int, emit: bool = True) -> int:
        by_num: Dict[int, dict] = {}
        kept: List[dict] = []
        discarded = 0
        for c in self.hands[seat]:
            if c.get("isJoker"):
                kept.append(c)
                continue
            num = int(c["num"])
            if num in by_num:
                pair_c = by_num.pop(num)
                discarded += 1
                self.prev_table_play = self.table_play
                self.table_play = {
                    "seat": seat,
                    "cards": [pair_c, c],
                    "label": f"{self.seat_display_name(seat)}: {c['label']}ペア成立！",
                }
            else:
                by_num[num] = c
        kept.extend(by_num.values())
        self.hands[seat] = kept
        if emit and discarded > 0:
            self._emit_event("bubble", "ペア成立！", seat=seat, sfx="special")
        return discarded

    def get_babanuki_target(self, drawer_seat: int) -> Optional[int]:
        for step in range(1, 4):
            cand = (drawer_seat + step) % 4
            if len(self.hands[cand]) > 0 and cand not in self.finish_order:
                return cand
        return None

    def execute_babanuki_draw(self, drawer_seat: int, card_idx: int) -> None:
        target_seat = self.get_babanuki_target(drawer_seat)
        if target_seat is None or not self.hands[target_seat]:
            return
        idx = max(0, min(len(self.hands[target_seat]) - 1, int(card_idx)))
        drawn = self.hands[target_seat].pop(idx)
        self.hands[drawer_seat].append(drawn)

        if drawn.get("isJoker"):
            self._emit_event("cutin", f"JOKER移動！ ({self.seat_display_name(target_seat)} -> {self.seat_display_name(drawer_seat)})", seat=drawer_seat, sfx="joker")
        else:
            self._emit_event("sfx", "", seat=drawer_seat, sfx="card")

        self._discard_babanuki_pairs(drawer_seat, emit=True)
        random.shuffle(self.hands[drawer_seat])

        for s in range(4):
            if len(self.hands[s]) == 0 and s not in self.finish_order:
                self.finish_order.append(s)
                self._emit_event("bubble", "抜け！", seat=s, sfx="special")

        active = [s for s in range(4) if len(self.hands[s]) > 0]
        if len(active) <= 1:
            for rem in active:
                if rem not in self.finish_order:
                    self.finish_order.append(rem)
            self._conclude_round()
            return

        self.current_turn = self._next_active_seat(drawer_seat)
        self.turn_since = time.time()

    def _conclude_round(self) -> None:
        titles = RANK_TITLES_DAIFUGO if self.mode == "daifugo" else RANK_TITLES_BABANUKI
        for idx, s in enumerate(self.finish_order[:4]):
            self.seat_ranks[s] = titles[idx] if idx < len(titles) else "平民"
        self.phase = "round_end"
        self.phase_since = time.time()
        winner_name = self.seat_display_name(self.finish_order[0]) if self.finish_order else ""
        self._emit_event("cutin", f"勝負あり！ 1位: {winner_name}", sfx="special")

    # ------------------------------------------------------------------------
    # Personalized View Snapshot Builder
    # ------------------------------------------------------------------------
    def build_view_for(self, pid: str) -> dict:
        my_seat: Optional[int] = None
        for s in range(4):
            if self.seats[s] == pid:
                my_seat = s
                break
        is_spectator = my_seat is None
        perspective_seat = my_seat if my_seat is not None else 0

        human_seats = sum(1 for s in range(4) if self.is_seat_human(s))
        spectator_list = [p["name"] for k, p in self.participants.items() if k not in self.seats]

        seats_info = []
        for s in range(4):
            rel_pos = (s - perspective_seat) % 4  # 0=Bottom(You), 1=Left, 2=Top, 3=Right
            fin_idx = self.finish_order.index(s) + 1 if s in self.finish_order else 0
            seats_info.append({
                "seat": s,
                "relPos": rel_pos,
                "name": self.seat_display_name(s),
                "isHuman": self.is_seat_human(s),
                "avatar": BOT_PROFILES[s]["avatar"],
                "rankTitle": self.seat_ranks[s],
                "cardCount": len(self.hands[s]),
                "finishRank": fin_idx,
                "isTurn": (self.phase == "playing" and self.current_turn == s),
            })

        my_hand = self.hands[perspective_seat] if not is_spectator else []

        playable_ids: List[str] = []
        suggested_ids: List[str] = []
        if not is_spectator and self.phase == "playing" and self.current_turn == my_seat and self.mode == "daifugo":
            legal = self.generate_legal_combos(my_seat)
            pset = {c["id"] for m in legal for c in m["cards"]}
            playable_ids = list(pset)
            best = self.choose_best_ai_move(my_seat)
            if best:
                suggested_ids = [c["id"] for c in best["cards"]]

        baba_target = None
        if self.mode == "babanuki" and self.phase == "playing":
            t_seat = self.get_babanuki_target(self.current_turn)
            if t_seat is not None:
                baba_target = {
                    "targetSeat": t_seat,
                    "targetName": self.seat_display_name(t_seat),
                    "cardCount": len(self.hands[t_seat]),
                }

        standings = []
        if self.phase == "round_end":
            for idx, s in enumerate(self.finish_order[:4]):
                standings.append({
                    "place": idx + 1,
                    "seat": s,
                    "name": self.seat_display_name(s),
                    "rankTitle": self.seat_ranks[s],
                    "isMe": (s == my_seat),
                })

        return {
            "mode": self.mode,
            "roomTitle": self.title,
            "roundNumber": self.round_number,
            "phase": self.phase,
            "mySeat": my_seat,
            "perspectiveSeat": perspective_seat,
            "isSpectator": is_spectator,
            "humanCount": human_seats,
            "spectatorCount": len(spectator_list),
            "hasOpenBotSeat": human_seats < 4,
            "currentTurn": self.current_turn,
            "turnRemainingSec": max(0, int(25 - (time.time() - self.turn_since))),
            "isRevolution": self.is_revolution,
            "isElevenBack": self.is_eleven_back,
            "lockedSuits": self.locked_suits or [],
            "rules": self.rules,
            "seats": seats_info,
            "myHand": my_hand,
            "playableIds": playable_ids,
            "suggestedIds": suggested_ids,
            "tablePlay": self.table_play,
            "prevTablePlay": self.prev_table_play,
            "babaTarget": baba_target,
            "standings": standings,
            "events": self.recent_events[-8:],
        }


# Singleton 1 Room per Rule Mode
ROOMS: Dict[str, TrumpRoom] = {
    "daifugo": TrumpRoom(
        "daifugo",
        "大富豪ルーム",
        "革命・8切り・11バック・しばり・スペ3返し対応（4人対戦＋観戦）",
    ),
    "babanuki": TrumpRoom(
        "babanuki",
        "ババ抜きルーム",
        "隣の人の手札から1枚引いてペアを揃えるシンプル心理戦（4人対戦＋観戦）",
    ),
}

_BG_TASK_STARTED = False


async def _broadcast_room(room: TrumpRoom) -> None:
    dead: List[str] = []
    for pid, ws in list(room.clients.items()):
        try:
            payload = room.build_view_for(pid)
            await ws.send_json(payload)
        except Exception:
            dead.append(pid)
    for pid in dead:
        room.clients.pop(pid, None)


async def _rooms_background_loop() -> None:
    while True:
        await asyncio.sleep(0.35)
        now = time.time()
        for room in ROOMS.values():
            should_broadcast = False
            async with room.lock:
                if room.prune_stale_participants():
                    should_broadcast = True

                if not room.participants:
                    continue

                if room.phase == "round_end":
                    if now - room.phase_since >= 6.0:
                        room.round_number += 1
                        room._start_new_round(reset_ranks=False)
                        should_broadcast = True
                elif room.phase == "playing":
                    turn_seat = room.current_turn
                    is_human_turn = room.is_seat_human(turn_seat)
                    elapsed = now - room.turn_since
                    if (not is_human_turn and elapsed >= 0.9) or (is_human_turn and elapsed >= 25.0):
                        if room.mode == "daifugo":
                            mv = room.choose_best_ai_move(turn_seat)
                            if mv:
                                room.execute_daifugo_play(turn_seat, mv)
                            else:
                                room.execute_daifugo_pass(turn_seat)
                        else:
                            t_seat = room.get_babanuki_target(turn_seat)
                            if t_seat is not None and room.hands[t_seat]:
                                t_hand = room.hands[t_seat]
                                pick_idx = random.randint(0, len(t_hand) - 1)
                                if len(t_hand) >= 2 and random.random() < 0.65:
                                    non_j = [i for i, c in enumerate(t_hand) if not c.get("isJoker")]
                                    if non_j:
                                        pick_idx = random.choice(non_j)
                                room.execute_babanuki_draw(turn_seat, pick_idx)
                        should_broadcast = True

            if should_broadcast:
                await _broadcast_room(room)


def _ensure_bg_loop() -> None:
    global _BG_TASK_STARTED
    if not _BG_TASK_STARTED:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_rooms_background_loop())
            _BG_TASK_STARTED = True
        except RuntimeError:
            pass


def _get_room(mode: str) -> TrumpRoom:
    m = (mode or "daifugo").strip().lower()
    return ROOMS["babanuki"] if m == "babanuki" else ROOMS["daifugo"]


@router.get("/trump/api/lobby")
async def trump_lobby_info():
    _ensure_bg_loop()
    rooms_list = []
    for key in ("daifugo", "babanuki"):
        r = ROOMS[key]
        async with r.lock:
            rooms_list.append(r.get_summary())
    return JSONResponse({"rooms": rooms_list}, headers={"Cache-Control": "no-store"})


@router.get("/trump/api/state/{mode}")
async def trump_get_state(mode: str, request: Request):
    _ensure_bg_loop()
    room = _get_room(mode)
    pid = (request.query_params.get("pid") or "").strip() or ("p_" + uuid.uuid4().hex[:12])
    name = (request.query_params.get("name") or "").strip()
    spectate_param = request.query_params.get("spectate")
    prefer_spec = (spectate_param == "1") if spectate_param is not None else None
    async with room.lock:
        room.touch_participant(pid, name=name if name else None, prefer_spectate=prefer_spec)
        data = room.build_view_for(pid)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@router.post("/trump/api/action/{mode}")
async def trump_post_action(mode: str, request: Request):
    _ensure_bg_loop()
    room = _get_room(mode)
    try:
        body = await request.json()
    except Exception:
        body = {}

    pid = str(body.get("pid") or "").strip() or ("p_" + uuid.uuid4().hex[:12])
    name = str(body.get("name") or "").strip()
    act = str(body.get("action") or "").strip()

    if act == "leave":
        async with room.lock:
            room.remove_participant(pid)
        await _broadcast_room(room)
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    async with room.lock:
        room.touch_participant(pid, name=name if name else None)
        await _handle_room_action_locked(room, pid, act, body)
        view = room.build_view_for(pid)

    await _broadcast_room(room)
    return JSONResponse(view, headers={"Cache-Control": "no-store"})


async def _handle_room_action_locked(room: TrumpRoom, pid: str, act: str, body: dict) -> None:
    my_seat: Optional[int] = None
    for s in range(4):
        if room.seats[s] == pid:
            my_seat = s
            break

    if act == "set_name":
        new_name = str(body.get("name") or "").strip()[:16]
        if new_name and pid in room.participants:
            room.participants[pid]["name"] = new_name

    elif act == "toggle_spectate":
        want_spec = bool(body.get("spectate"))
        room.touch_participant(pid, prefer_spectate=want_spec)

    elif act == "play" and room.mode == "daifugo" and room.phase == "playing" and my_seat == room.current_turn:
        card_ids = set(body.get("cardIds") or [])
        selected = [c for c in room.hands[my_seat] if c["id"] in card_ids]
        combo = room.analyze_combo(selected)
        if room.is_legal_play(combo):
            room.execute_daifugo_play(my_seat, combo)

    elif act == "pass" and room.mode == "daifugo" and room.phase == "playing" and my_seat == room.current_turn:
        if room.table_play is not None:
            room.execute_daifugo_pass(my_seat)

    elif act == "draw" and room.mode == "babanuki" and room.phase == "playing" and my_seat == room.current_turn:
        idx = int(body.get("cardIndex") or 0)
        room.execute_babanuki_draw(my_seat, idx)

    elif act == "shuffle" and room.mode == "babanuki" and my_seat is not None:
        random.shuffle(room.hands[my_seat])
        room._emit_event("sfx", "", seat=my_seat, sfx="card")

    elif act == "next_round" and room.phase == "round_end":
        room.round_number += 1
        room._start_new_round(reset_ranks=False)

    elif act == "reset_match":
        room._start_new_round(reset_ranks=True)

    elif act == "update_rules" and isinstance(body.get("rules"), dict):
        for k in list(room.rules.keys()):
            if k in body["rules"]:
                room.rules[k] = bool(body["rules"][k])


@router.websocket("/trump/ws/{mode}")
async def trump_ws_endpoint(websocket: WebSocket, mode: str):
    _ensure_bg_loop()
    room = _get_room(mode)
    pid = (websocket.query_params.get("pid") or "").strip() or ("p_" + uuid.uuid4().hex[:12])
    name = (websocket.query_params.get("name") or "").strip()
    spectate_param = websocket.query_params.get("spectate")
    prefer_spec = (spectate_param == "1") if spectate_param is not None else None
    await websocket.accept()

    async with room.lock:
        room.clients[pid] = websocket
        room.touch_participant(pid, name=name if name else None, prefer_spectate=prefer_spec)
    await _broadcast_room(room)

    try:
        while True:
            data = await websocket.receive_json()
            act = str(data.get("action") or "").strip()
            async with room.lock:
                room.touch_participant(pid)
                await _handle_room_action_locked(room, pid, act, data)
            await _broadcast_room(room)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        async with room.lock:
            room.clients.pop(pid, None)
        await _broadcast_room(room)


# ============================================================================
# Simple, Clean, Easy-to-Play HTML UI (Room Select Lobby + 4-Player Table)
# ============================================================================

_HTML_PAGE = r"""<!doctype html>
<html lang="ja" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Google School Hub — トランプ</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="stylesheet" href="/design-system/m3.css">
<script src="/design-system/m3.js"></script>
<style>
:root {
  --bg: var(--md-sys-color-surface);
  --surface: var(--md-sys-color-surface-container);
  --table-green: #14532d;
  --table-border: #3f230f;
  --text: var(--md-sys-color-on-surface);
  --muted: var(--md-sys-color-on-surface-variant);
  --accent: var(--md-sys-color-primary);
  --gold: #f59e0b;
}
* { box-sizing: border-box; user-select: none; -webkit-user-select: none; }
html, body {
  margin: 0; padding: 0;
  width: 100%; height: 100%;
  overflow: hidden;
  background: var(--bg);
  color: var(--text);
  font-family: var(--md-sys-typescale-font-plain);
}

/* =========================================================================
 * 1. Top Header Bar (M3 Top App Bar)
 * ========================================================================= */
.topbar {
  min-height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 16px;
  gap: 8px;
  background: var(--md-sys-color-surface-container);
  border-bottom: 1px solid var(--md-sys-color-outline-variant);
  z-index: 50;
}
.topbar-left, .topbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.btn-top {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border-radius: var(--md-sys-shape-corner-full);
  background: var(--md-sys-color-secondary-container);
  border: 1px solid var(--md-sys-color-outline-variant);
  color: var(--md-sys-color-on-secondary-container);
  text-decoration: none;
  font-size: 13px;
  font-weight: 700;
  white-space: nowrap;
  flex-shrink: 0;
  cursor: pointer;
  transition: filter 0.15s;
}
.btn-top:hover { filter: brightness(0.95); }
.room-badge {
  font-size: 15px;
  font-weight: 700;
  color: var(--md-sys-color-primary);
  margin-left: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.meta-tag {
  font-size: 12px;
  font-weight: 700;
  padding: 4px 12px;
  border-radius: var(--md-sys-shape-corner-full);
  background: var(--md-sys-color-surface-container-high);
  border: 1px solid var(--md-sys-color-outline-variant);
  color: var(--md-sys-color-on-surface-variant);
  white-space: nowrap;
  flex-shrink: 0;
}

/* =========================================================================
 * 2. Room Selection Lobby Screen (M3 Surface Hierarchy)
 * ========================================================================= */
.lobby-screen {
  position: fixed;
  top: 56px; left: 0; right: 0; bottom: 0;
  background: var(--md-sys-color-surface);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  z-index: 45;
  overflow-y: auto;
}
.lobby-screen.hidden { display: none; }
.lobby-container {
  width: min(94vw, 720px);
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.lobby-header {
  text-align: center;
}
.lobby-header h1 {
  margin: 0 0 8px;
  font-size: 26px;
  font-weight: 800;
  color: var(--md-sys-color-on-surface);
}
.lobby-header p {
  margin: 0;
  font-size: 14px;
  color: var(--muted);
}

.name-box {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  background: var(--md-sys-color-surface-container-low);
  border: 1px solid var(--md-sys-color-outline-variant);
  padding: 14px 18px;
  border-radius: var(--md-sys-shape-corner-large);
  box-shadow: var(--md-sys-elevation-1);
}
.name-box label {
  font-size: 13px;
  font-weight: 700;
  color: var(--md-sys-color-on-surface-variant);
}
.name-input {
  background: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline);
  color: var(--md-sys-color-on-surface);
  padding: 8px 14px;
  border-radius: var(--md-sys-shape-corner-small);
  font-size: 14px;
  font-weight: 700;
  width: 210px;
  outline: none;
}
.name-input:focus {
  border-color: var(--md-sys-color-primary);
  box-shadow: 0 0 0 2px var(--md-sys-color-primary-container);
}

.room-cards-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
}
.room-card {
  background: var(--md-sys-color-surface-container-low);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-extra-large);
  padding: 22px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 14px;
  box-shadow: var(--md-sys-elevation-1);
  transition: border-color 0.18s, transform 0.18s, box-shadow 0.18s;
}
.room-card:hover {
  border-color: var(--md-sys-color-primary);
  transform: translateY(-2px);
  box-shadow: var(--md-sys-elevation-2);
}
.room-card-title {
  font-size: 20px;
  font-weight: 800;
  margin: 0 0 6px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.room-card-desc {
  font-size: 13px;
  color: var(--muted);
  line-height: 1.5;
  margin: 0;
}
.room-stats-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.stat-pill {
  font-size: 12px;
  font-weight: 700;
  padding: 4px 10px;
  border-radius: var(--md-sys-shape-corner-full);
  background: var(--md-sys-color-tertiary-container);
  color: var(--md-sys-color-on-tertiary-container);
}
.stat-pill.full {
  background: var(--md-sys-color-warning-container);
  color: var(--md-sys-color-on-warning-container);
}
.stat-pill.spec {
  background: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
}
.room-members {
  font-size: 12px;
  color: var(--md-sys-color-on-surface-variant);
  background: var(--md-sys-color-surface-container-high);
  padding: 8px 12px;
  border-radius: var(--md-sys-shape-corner-medium);
}
.room-actions {
  display: flex;
  gap: 8px;
}
.btn-join-room {
  flex: 1;
  padding: 11px 16px;
  border-radius: var(--md-sys-shape-corner-full);
  border: 0;
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
  background: var(--md-sys-color-primary);
  color: var(--md-sys-color-on-primary);
  transition: filter 0.15s;
}
.btn-join-room:hover { filter: brightness(1.06); }
.btn-spec-room {
  padding: 11px 16px;
  border-radius: var(--md-sys-shape-corner-full);
  border: 1px solid var(--md-sys-color-outline);
  background: var(--md-sys-color-surface-container-highest);
  color: var(--md-sys-color-on-surface);
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
}
.btn-spec-room:hover { filter: brightness(0.96); }

/* =========================================================================
 * 3. Simple, Easy-to-Play 4-Player Game Screen
 * ========================================================================= */
.game-screen {
  position: fixed;
  top: 56px; left: 0; right: 0; bottom: 0;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  background: var(--bg);
  padding: 10px 14px 12px;
}
.game-screen.hidden { display: none; }

/* Table Board Area (Holds North, West, East, and Center Play Area) */
.table-board {
  position: relative;
  flex: 1;
  width: min(100%, 980px);
  margin: 0 auto;
  background: radial-gradient(ellipse at center, #166534 0%, #14532d 70%, #064e3b 100%);
  border: 8px solid #451a03;
  border-radius: 28px;
  box-shadow: inset 0 0 40px rgba(0,0,0,0.45), var(--md-sys-elevation-3);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

/* Status Pills at Top of Table */
.table-rules-bar {
  position: absolute;
  top: 10px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: center;
  z-index: 15;
}
.status-pill {
  padding: 4px 12px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 800;
  background: rgba(15, 23, 42, 0.88);
  border: 1px solid rgba(255,255,255,0.22);
  color: #fde047;
}
.status-pill.alert {
  background: var(--md-sys-color-error);
  color: var(--md-sys-color-on-error);
  border-color: var(--md-sys-color-error-container);
}

/* Seated Opponents (Top, Left, Right) */
.opp-seat {
  position: absolute;
  background: rgba(15, 23, 42, 0.86);
  border: 2px solid rgba(255,255,255,0.18);
  border-radius: 14px;
  padding: 8px 12px;
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 155px;
  color: #f8fafc;
  z-index: 10;
  transition: border-color 0.2s, box-shadow 0.2s;
}
.opp-seat.active-turn {
  border-color: #fde047;
  box-shadow: 0 0 0 3px rgba(253, 224, 71, 0.45);
}
.opp-north { top: 44px; left: 50%; transform: translateX(-50%); }
.opp-west  { left: 14px; top: 50%; transform: translateY(-50%); }
.opp-east  { right: 14px; top: 50%; transform: translateY(-50%); }

.opp-avatar {
  width: 40px; height: 40px;
  border-radius: 50%;
  background: #334155;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 22px;
  flex-shrink: 0;
}
.opp-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.opp-name-line {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 13px;
  font-weight: 800;
}
.opp-rank {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 999px;
  background: rgba(245, 158, 11, 0.25);
  color: #fde047;
}
.opp-cards-left {
  font-size: 12px;
  font-weight: 800;
  color: #cbd5e1;
}
.opp-cards-left.danger {
  color: #fca5a5;
}
.opp-bubble {
  position: absolute;
  top: -28px;
  left: 50%;
  transform: translateX(-50%);
  background: #fff;
  color: #0f172a;
  font-size: 12px;
  font-weight: 900;
  padding: 3px 10px;
  border-radius: 999px;
  white-space: nowrap;
  box-shadow: 0 4px 12px rgba(0,0,0,0.4);
  display: none;
}
.opp-bubble.show { display: block; }

/* Center Field (Played Cards OR Babanuki Draw Picker) */
.center-field {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  z-index: 12;
  padding: 12px;
}
.played-cards-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 108px;
}
.play-caption {
  font-size: 13px;
  font-weight: 800;
  color: #fef08a;
  background: rgba(15, 23, 42, 0.85);
  padding: 4px 14px;
  border-radius: 999px;
}

/* Babanuki Pick Cards in Center */
.baba-draw-box {
  background: rgba(15, 23, 42, 0.92);
  border: 2px solid #fde047;
  border-radius: 16px;
  padding: 14px 18px;
  text-align: center;
  display: none;
  flex-direction: column;
  gap: 10px;
  max-width: 90%;
  color: #f8fafc;
}
.baba-draw-box.show { display: flex; }
.baba-cards-list {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  flex-wrap: wrap;
}
.baba-back-card {
  width: 54px;
  height: 80px;
  border-radius: 8px;
  background: repeating-linear-gradient(45deg, #991b1b 0 6px, #dc2626 6px 12px);
  border: 2px solid #fff;
  cursor: pointer;
  transition: transform 0.14s, border-color 0.14s;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
}
.baba-back-card:hover {
  transform: translateY(-10px);
  border-color: #fde047;
}

/* =========================================================================
 * 4. Bottom Player Controls & Easy-to-Click Card Hand (M3 Surface Container)
 * ========================================================================= */
.player-bottom-panel {
  width: min(100%, 980px);
  margin: 10px auto 0;
  background: var(--md-sys-color-surface-container-low);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-large);
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  box-shadow: var(--md-sys-elevation-1);
}
.action-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}
.my-info-pill {
  font-size: 14px;
  font-weight: 800;
  color: var(--md-sys-color-on-surface);
}
.action-buttons {
  display: flex;
  align-items: center;
  gap: 8px;
}
.btn-act {
  padding: 9px 18px;
  border-radius: var(--md-sys-shape-corner-full);
  border: 0;
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
  transition: opacity 0.15s, transform 0.1s;
}
.btn-act:disabled {
  opacity: 0.38;
  cursor: not-allowed;
}
.btn-act.play { background: var(--md-sys-color-primary); color: var(--md-sys-color-on-primary); }
.btn-act.pass { background: var(--md-sys-color-surface-container-highest); color: var(--md-sys-color-on-surface); }
.btn-act.hint { background: var(--md-sys-color-secondary-container); color: var(--md-sys-color-on-secondary-container); }

/* Clean, Well-Spaced Hand Cards */
.hand-cards-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  flex-wrap: wrap;
  min-height: 104px;
  padding-top: 6px;
}
.card-item {
  width: 66px;
  height: 96px;
  border-radius: 9px;
  background: #ffffff;
  border: 2px solid #cbd5e1;
  color: #0f172a;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: 5px 6px;
  cursor: pointer;
  position: relative;
  transition: transform 0.12s, border-color 0.12s, box-shadow 0.12s;
  box-shadow: 0 3px 8px rgba(0,0,0,0.35);
}
.card-item.red { color: #dc2626; }
.card-item.black { color: #0f172a; }
.card-item.joker {
  background: #fef9c3;
  border-color: #f59e0b;
  color: #9a3412;
}
.card-item:hover {
  transform: translateY(-6px);
}
.card-item.selected {
  transform: translateY(-14px);
  border: 3px solid #22c55e;
  box-shadow: 0 6px 16px rgba(34, 197, 94, 0.5);
}
.card-item.unplayable {
  opacity: 0.45;
}
.c-top {
  font-size: 16px;
  font-weight: 900;
  line-height: 1;
}
.c-center {
  font-size: 26px;
  text-align: center;
  line-height: 1;
}
.c-bot {
  font-size: 12px;
  font-weight: 800;
  text-align: right;
  line-height: 1;
}

/* Cut-in Toast */
.toast-banner {
  position: fixed;
  top: 42%; left: 50%;
  transform: translate(-50%, -50%);
  background: var(--md-sys-color-inverse-surface);
  border: 2px solid var(--md-sys-color-primary);
  color: var(--md-sys-color-inverse-on-surface);
  padding: 12px 28px;
  border-radius: var(--md-sys-shape-corner-large);
  font-size: 22px;
  font-weight: 900;
  z-index: 80;
  pointer-events: none;
  display: none;
}
.toast-banner.show { display: block; }

/* Modals (M3 Dialog) */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: var(--md-sys-color-scrim);
  backdrop-filter: blur(4px);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 90;
  padding: 16px;
}
.modal-overlay.show { display: flex; }
.modal-box {
  width: min(92vw, 420px);
  background: var(--md-sys-color-surface-container-high);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-extra-large);
  padding: 24px;
  text-align: center;
  box-shadow: var(--md-sys-elevation-3);
}
.rank-row-item {
  display: flex;
  justify-content: space-between;
  padding: 9px 12px;
  border-radius: var(--md-sys-shape-corner-small);
  background: var(--md-sys-color-surface-container);
  margin-bottom: 6px;
  font-size: 14px;
  font-weight: 700;
}
.rule-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 0;
  border-bottom: 1px solid var(--md-sys-color-outline-variant);
  font-size: 13px;
}

@media (max-width: 640px) {
  .topbar {
    min-height: 48px;
    padding: 6px 8px;
    gap: 4px;
    flex-wrap: wrap;
  }
  .topbar-left, .topbar-right {
    gap: 4px;
    flex-wrap: wrap;
  }
  .btn-top {
    padding: 4px 8px;
    font-size: 11.5px;
  }
  .room-badge {
    font-size: 12.5px;
    margin-left: 2px;
  }
  .meta-tag {
    font-size: 11px;
    padding: 3px 8px;
  }
  .lobby-screen {
    top: 52px;
    align-items: flex-start;
    padding: 14px 12px 28px;
  }
  .lobby-header h1 {
    font-size: 21px;
  }
  .name-box {
    padding: 10px 12px;
    gap: 8px;
    flex-wrap: wrap;
  }
  .game-screen {
    top: 52px;
    padding: 6px 8px 8px;
  }
  .opp-seat {
    min-width: 96px;
    padding: 4px 7px;
    gap: 6px;
    border-radius: 10px;
  }
  .opp-north { top: 34px; left: 50%; transform: translateX(-50%); }
  .opp-west  { top: 86px; left: 6px; transform: none; }
  .opp-east  { top: 86px; right: 6px; transform: none; }
  .opp-avatar { width: 26px; height: 26px; font-size: 14px; }
  .opp-name-line { font-size: 11.5px; }
  .opp-cards-left { font-size: 11px; }
  .center-field { margin-top: 44px; padding: 6px; }
  .player-bottom-panel { padding: 8px 10px; gap: 6px; margin-top: 6px; }
  .btn-act { padding: 7px 12px; font-size: 12px; }
  .hand-cards-row { min-height: 78px; gap: 4px; }
  .card-item { width: 48px; height: 72px; padding: 4px 5px; }
  .c-top { font-size: 13px; }
  .c-center { font-size: 18px; }
}
</style>
</head>
<body>

<!-- Top Navigation Bar (M3 Top App Bar) -->
<header class="topbar">
  <div class="topbar-left">
    <a class="btn-top" href="/">← School Hub</a>
    <button type="button" class="btn-top" id="btnBackLobby" onclick="returnToLobby()" style="display:none">ルーム選択へ</button>
    <span class="room-badge" id="headerRoomTitle">トランプ ルーム選択</span>
  </div>
  <div class="topbar-right">
    <span class="meta-tag" id="headerSeatMeta" style="display:none">着席 1/4</span>
    <span class="meta-tag" id="headerSpecMeta" style="display:none">観戦 0</span>
    <button type="button" class="btn-top" id="btnRules" onclick="toggleRulesModal(true)" style="display:none">ルール</button>
    <button type="button" class="btn-top" id="btnSound" onclick="toggleSound()">音 ON</button>
    <button type="button" class="btn-top" onclick="window.SchoolHubM3 && window.SchoolHubM3.openSettings()">M3設定</button>
    <button type="button" class="btn-top" onclick="window.SchoolHubM3 && window.SchoolHubM3.toggleTheme()">テーマ</button>
  </div>
</header>

<!-- 1. ROOM SELECTION LOBBY SCREEN (Shown first on entry) -->
<section class="lobby-screen" id="lobbyScreen">
  <div class="lobby-container">
    <div class="lobby-header">
      <h1>トランプ オンラインロビー</h1>
      <p>遊びたいゲームルールを選んで入室してください（各ルール全1部屋・満席時は自動で観戦モードになります）</p>
    </div>

    <div class="name-box">
      <label for="playerNameInput">あなたの名前:</label>
      <input type="text" id="playerNameInput" class="name-input" maxlength="16" placeholder="名前を入力">
    </div>

    <div class="room-cards-grid" id="lobbyRoomsGrid">
      <!-- Populated dynamically from /trump/api/lobby -->
      <div class="room-card">
        <div>
          <div class="room-card-title"><span>大富豪ルーム</span></div>
          <p class="room-card-desc">革命・8切り・11バック・しばり・スペ3返し対応（4人対戦＋観戦）</p>
        </div>
        <div class="room-actions">
          <button type="button" class="btn-join-room" onclick="enterRoom('daifugo', false)">入室して遊ぶ</button>
          <button type="button" class="btn-spec-room" onclick="enterRoom('daifugo', true)">観戦</button>
        </div>
      </div>

      <div class="room-card">
        <div>
          <div class="room-card-title"><span>ババ抜きルーム</span></div>
          <p class="room-card-desc">隣の人の手札から1枚引いてペアを揃えるシンプル心理戦（4人対戦＋観戦）</p>
        </div>
        <div class="room-actions">
          <button type="button" class="btn-join-room" onclick="enterRoom('babanuki', false)">入室して遊ぶ</button>
          <button type="button" class="btn-spec-room" onclick="enterRoom('babanuki', true)">観戦</button>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- 2. SIMPLE 4-PLAYER GAME SCREEN -->
<main class="game-screen hidden" id="gameScreen">
  <div class="table-board">
    <div class="table-rules-bar" id="tableRulesBar"></div>

    <!-- North (Across) -->
    <div class="opp-seat opp-north" id="seat-rel-2">
      <div class="opp-bubble" id="bubble-rel-2"></div>
      <div class="opp-avatar" id="av-rel-2">L</div>
      <div class="opp-info">
        <div class="opp-name-line">
          <span id="name-rel-2">レイ</span>
          <span class="opp-rank" id="rank-rel-2">平民</span>
        </div>
        <div class="opp-cards-left" id="cnt-rel-2">残り 13枚</div>
      </div>
    </div>

    <!-- West (Left) -->
    <div class="opp-seat opp-west" id="seat-rel-1">
      <div class="opp-bubble" id="bubble-rel-1"></div>
      <div class="opp-avatar" id="av-rel-1">K</div>
      <div class="opp-info">
        <div class="opp-name-line">
          <span id="name-rel-1">カイ</span>
          <span class="opp-rank" id="rank-rel-1">平民</span>
        </div>
        <div class="opp-cards-left" id="cnt-rel-1">残り 13枚</div>
      </div>
    </div>

    <!-- East (Right) -->
    <div class="opp-seat opp-east" id="seat-rel-3">
      <div class="opp-bubble" id="bubble-rel-3"></div>
      <div class="opp-avatar" id="av-rel-3">M</div>
      <div class="opp-info">
        <div class="opp-name-line">
          <span id="name-rel-3">ミナ</span>
          <span class="opp-rank" id="rank-rel-3">平民</span>
        </div>
        <div class="opp-cards-left" id="cnt-rel-3">残り 13枚</div>
      </div>
    </div>

    <!-- Center Play Area -->
    <div class="center-field">
      <div class="played-cards-row" id="centerPlayedCards"></div>
      <div class="play-caption" id="centerPlayCaption" style="display:none"></div>

      <!-- Babanuki Draw Box -->
      <div class="baba-draw-box" id="babaDrawBox">
        <div style="font-size:14px;font-weight:900;color:#fde047" id="babaDrawPrompt">隣の人のカードを1枚クリックして引いてください</div>
        <div class="baba-cards-list" id="babaCardsList"></div>
      </div>
    </div>
  </div>

  <!-- Bottom Player Hand & Simple Controls -->
  <div class="player-bottom-panel">
    <div class="action-bar">
      <div class="my-info-pill" id="myStatusText">あなた (平民)</div>
      <div class="action-buttons">
        <button type="button" class="btn-act hint" id="btnHint" onclick="selectSuggestedMove()">おすすめ</button>
        <button type="button" class="btn-act play" id="btnPlay" onclick="commitPlayerMove()" disabled>カードを出す</button>
        <button type="button" class="btn-act pass" id="btnPass" onclick="playerPassTurn()" disabled>パス</button>
        <button type="button" class="btn-act hint" id="btnShuffle" onclick="shufflePlayerHand()" style="display:none">シャッフル</button>
        <button type="button" class="btn-act play" id="btnTakeSeat" onclick="switchSpectate(false)" style="display:none">空き席に座る</button>
        <button type="button" class="btn-act pass" id="btnGoSpectate" onclick="switchSpectate(true)">観戦に回る</button>
      </div>
    </div>
    <div class="hand-cards-row" id="myHandRow"></div>
  </div>
</main>

<!-- Toast Cut-in -->
<div class="toast-banner" id="toastBanner"></div>

<!-- Round Result Modal -->
<div class="modal-overlay" id="resultModal">
  <div class="modal-box">
    <h2 style="margin:0 0 8px;color:#fde047">ラウンド結果</h2>
    <div id="resultList" style="margin-bottom:14px"></div>
    <button type="button" class="btn-act play" onclick="sendRoomAction({action:'next_round'})">次のラウンドへ進む</button>
  </div>
</div>

<!-- Rules Modal -->
<div class="modal-overlay" id="rulesModal">
  <div class="modal-box">
    <h3 style="margin:0 0 12px;color:#fde047">大富豪ルール設定</h3>
    <div class="rule-row"><span>革命 (4枚以上同時出し)</span><input type="checkbox" id="ruleRev" checked></div>
    <div class="rule-row"><span>8切り (8で場流し)</span><input type="checkbox" id="rule8Giri" checked></div>
    <div class="rule-row"><span>11バック (Jで一時強さ逆転)</span><input type="checkbox" id="rule11Back" checked></div>
    <div class="rule-row"><span>しばり (同マーク連続)</span><input type="checkbox" id="ruleShibari" checked></div>
    <div class="rule-row"><span>スペ3返し (JOKER単騎に♠3)</span><input type="checkbox" id="ruleSpade3" checked></div>
    <div class="rule-row"><span>階段 (同マーク3枚以上連番)</span><input type="checkbox" id="ruleStairs" checked></div>
    <div class="rule-row"><span>都落ち (大富豪1位陥落)</span><input type="checkbox" id="ruleMiyako" checked></div>
    <div style="margin-top:14px;display:flex;justify-content:space-between">
      <button type="button" class="btn-act pass" onclick="sendRoomAction({action:'reset_match'});toggleRulesModal(false)">卓リセット</button>
      <button type="button" class="btn-act play" onclick="saveRulesAndClose()">保存して閉じる</button>
    </div>
  </div>
</div>

<script>
(() => {
  let playerId = localStorage.getItem("sh_trump_pid");
  if (!playerId) {
    playerId = "p_" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
    localStorage.setItem("sh_trump_pid", playerId);
  }
  let playerName = localStorage.getItem("sh_trump_name") || `プレイヤー${Math.floor(100 + Math.random() * 899)}`;
  const nameInput = document.getElementById("playerNameInput");
  nameInput.value = playerName;
  nameInput.addEventListener("change", () => {
    const v = nameInput.value.trim().slice(0, 16);
    if (v) {
      playerName = v;
      localStorage.setItem("sh_trump_name", playerName);
    }
  });

  let inLobby = true;
  let currentMode = null;
  let roomState = null;
  let selectedCardIds = new Set();
  let lastEventSeq = 0;
  let ws = null;
  let pollTimer = null;
  let lobbyTimer = null;
  let soundEnabled = true;
  let audioCtx = null;

  function playSfx(type) {
    if (!soundEnabled) return;
    try {
      if (!audioCtx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (AC) audioCtx = new AC();
      }
      if (!audioCtx) return;
      if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
      const now = audioCtx.currentTime;
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      if (type === "card") {
        osc.type = "triangle";
        osc.frequency.setValueAtTime(320, now);
        osc.frequency.exponentialRampToValueAtTime(100, now + 0.06);
        gain.gain.setValueAtTime(0.22, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.065);
        osc.connect(gain).connect(audioCtx.destination);
        osc.start(now); osc.stop(now + 0.07);
      } else if (type === "select") {
        osc.type = "sine";
        osc.frequency.setValueAtTime(520, now);
        gain.gain.setValueAtTime(0.1, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.04);
        osc.connect(gain).connect(audioCtx.destination);
        osc.start(now); osc.stop(now + 0.045);
      } else if (type === "pass") {
        osc.type = "sine";
        osc.frequency.setValueAtTime(240, now);
        osc.frequency.exponentialRampToValueAtTime(160, now + 0.1);
        gain.gain.setValueAtTime(0.14, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.11);
        osc.connect(gain).connect(audioCtx.destination);
        osc.start(now); osc.stop(now + 0.12);
      } else if (type === "special" || type === "joker") {
        osc.type = "triangle";
        osc.frequency.setValueAtTime(type === "joker" ? 190 : 587, now);
        gain.gain.setValueAtTime(0.2, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.22);
        osc.connect(gain).connect(audioCtx.destination);
        osc.start(now); osc.stop(now + 0.24);
      }
    } catch (e) {}
  }

  window.toggleSound = function() {
    soundEnabled = !soundEnabled;
    document.getElementById("btnSound").textContent = soundEnabled ? "音 ON" : "音 OFF";
  };

  /* =========================================================================
   * 1. Lobby Fetching & Room Entry
   * ========================================================================= */
  async function refreshLobby() {
    if (!inLobby) return;
    try {
      const r = await fetch("/trump/api/lobby", { cache: "no-store" });
      if (!r.ok) return;
      const data = await r.json();
      const grid = document.getElementById("lobbyRoomsGrid");
      grid.innerHTML = "";
      for (const rm of data.rooms || []) {
        const card = document.createElement("div");
        card.className = "room-card";
        const statusCls = rm.isFull ? "stat-pill full" : "stat-pill";
        const statusTxt = rm.isFull
          ? `満席 4/4人 (自動で観戦になります)`
          : `着席 ${rm.humanCount}/4人 (空き席はAIが補完)`;
        const btnMainTxt = rm.isFull ? "満席のため観戦で入室" : "入室して遊ぶ";
        card.innerHTML = `
          <div>
            <div class="room-card-title">
              <span>${rm.title}</span>
              <span style="font-size:12px;color:#94a3b8">第${rm.roundNumber}戦</span>
            </div>
            <p class="room-card-desc">${rm.description}</p>
          </div>
          <div class="room-stats-row">
            <span class="${statusCls}">${statusTxt}</span>
            <span class="stat-pill spec">観戦 ${rm.spectatorCount}人</span>
          </div>
          <div class="room-members">現在の卓: ${rm.seatedNames.join(" / ")}</div>
          <div class="room-actions">
            <button type="button" class="btn-join-room" data-mode="${rm.mode}" data-spec="${rm.isFull ? '1' : '0'}">${btnMainTxt}</button>
            <button type="button" class="btn-spec-room" data-mode="${rm.mode}" data-spec="1">観戦</button>
          </div>
        `;
        card.querySelectorAll("button").forEach(btn => {
          btn.addEventListener("click", () => {
            enterRoom(btn.dataset.mode, btn.dataset.spec === "1");
          });
        });
        grid.appendChild(card);
      }
    } catch (e) {}
  }

  window.enterRoom = function(mode, spectate) {
    const v = nameInput.value.trim().slice(0, 16);
    if (v) {
      playerName = v;
      localStorage.setItem("sh_trump_name", playerName);
    }
    inLobby = false;
    currentMode = mode;
    selectedCardIds.clear();
    lastEventSeq = 0;

    document.getElementById("lobbyScreen").classList.add("hidden");
    document.getElementById("gameScreen").classList.remove("hidden");
    document.getElementById("btnBackLobby").style.display = "inline-flex";
    document.getElementById("headerSeatMeta").style.display = "inline-flex";
    document.getElementById("headerSpecMeta").style.display = "inline-flex";
    document.getElementById("btnRules").style.display = mode === "daifugo" ? "inline-flex" : "none";

    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
    const specFlag = spectate ? "1" : "0";
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${location.host}/trump/ws/${mode}?pid=${encodeURIComponent(playerId)}&name=${encodeURIComponent(playerName)}&spectate=${specFlag}`;
    try {
      ws = new WebSocket(wsUrl);
      ws.onmessage = (ev) => {
        try { applyRoomState(JSON.parse(ev.data)); } catch (e) {}
      };
    } catch (e) {}

    fetchRoomState(specFlag);
    clearInterval(pollTimer);
    pollTimer = setInterval(() => fetchRoomState(), 1500);
  };

  window.returnToLobby = async function() {
    const prevMode = currentMode;
    inLobby = true;
    currentMode = null;
    clearInterval(pollTimer);
    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
    if (prevMode) {
      try {
        await fetch(`/trump/api/action/${prevMode}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "leave", pid: playerId }),
        });
      } catch (e) {}
    }
    document.getElementById("gameScreen").classList.add("hidden");
    document.getElementById("lobbyScreen").classList.remove("hidden");
    document.getElementById("btnBackLobby").style.display = "none";
    document.getElementById("headerSeatMeta").style.display = "none";
    document.getElementById("headerSpecMeta").style.display = "none";
    document.getElementById("btnRules").style.display = "none";
    document.getElementById("headerRoomTitle").textContent = "トランプ ルーム選択";
    refreshLobby();
  };

  async function fetchRoomState(specFlag) {
    if (inLobby || !currentMode) return;
    const sp = specFlag !== undefined ? `&spectate=${specFlag}` : "";
    try {
      const r = await fetch(`/trump/api/state/${currentMode}?pid=${encodeURIComponent(playerId)}&name=${encodeURIComponent(playerName)}${sp}`, { cache: "no-store" });
      if (r.ok) applyRoomState(await r.json());
    } catch (e) {}
  }

  window.sendRoomAction = async function(payload) {
    if (!currentMode) return;
    const body = { ...payload, pid: playerId, name: playerName };
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(body));
      return;
    }
    try {
      const r = await fetch(`/trump/api/action/${currentMode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (r.ok) applyRoomState(await r.json());
    } catch (e) {}
  };

  window.switchSpectate = function(wantSpec) {
    sendRoomAction({ action: "toggle_spectate", spectate: Boolean(wantSpec) });
  };

  /* =========================================================================
   * 2. Render Game State Cleanly
   * ========================================================================= */
  function createSimpleCard(card, extraCls = "") {
    const div = document.createElement("div");
    div.className = `card-item ${card.isJoker ? "joker" : card.color} ${extraCls}`.trim();
    if (card.isJoker) {
      div.innerHTML = `<div class="c-top">JOKER</div><div class="c-center">★</div><div class="c-bot">★</div>`;
    } else {
      div.innerHTML = `<div class="c-top">${card.label}</div><div class="c-center">${card.suitSymbol}</div><div class="c-bot">${card.label}</div>`;
    }
    return div;
  }

  function showToast(text) {
    const el = document.getElementById("toastBanner");
    el.textContent = text;
    el.classList.add("show");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove("show"), 1350);
  }

  function showOppBubble(relPos, text) {
    const b = document.getElementById(`bubble-rel-${relPos}`);
    if (!b) return;
    b.textContent = text;
    b.classList.add("show");
    clearTimeout(b._t);
    b._t = setTimeout(() => b.classList.remove("show"), 1600);
  }

  function applyRoomState(state) {
    roomState = state;

    if (Array.isArray(state.events)) {
      for (const ev of state.events) {
        if (ev.seq > lastEventSeq) {
          lastEventSeq = ev.seq;
          if (ev.sfx) playSfx(ev.sfx);
          if (ev.kind === "cutin" && ev.text) showToast(ev.text);
          if (ev.kind === "bubble" && ev.text && ev.seat >= 0) {
            const rel = (ev.seat - state.perspectiveSeat + 4) % 4;
            if (rel !== 0) showOppBubble(rel, ev.text);
          }
        }
      }
    }

    document.getElementById("headerRoomTitle").textContent = `${state.roomTitle} (第${state.roundNumber}戦)`;
    document.getElementById("headerSeatMeta").textContent = `着席 ${state.humanCount}/4人`;
    document.getElementById("headerSpecMeta").textContent = `観戦 ${state.spectatorCount}人`;

    // Rules / Turn Bar
    const rBar = document.getElementById("tableRulesBar");
    rBar.innerHTML = "";
    const turnSeatObj = state.seats.find(s => s.seat === state.currentTurn);
    const turnPill = document.createElement("span");
    turnPill.className = "status-pill";
    turnPill.textContent = (!state.isSpectator && state.currentTurn === state.mySeat)
      ? `あなたの番です (${state.turnRemainingSec}秒)`
      : `${turnSeatObj ? turnSeatObj.name : ""}の番 (${state.turnRemainingSec}秒)`;
    rBar.appendChild(turnPill);

    if (state.isSpectator) {
      const sp = document.createElement("span");
      sp.className = "status-pill alert";
      sp.textContent = "観戦モード中";
      rBar.appendChild(sp);
    }
    if (state.mode === "daifugo") {
      if (state.isRevolution) {
        const p = document.createElement("span");
        p.className = "status-pill alert";
        p.textContent = "革命中 (3が最強)";
        rBar.appendChild(p);
      }
      if (state.isElevenBack) {
        const p = document.createElement("span");
        p.className = "status-pill alert";
        p.textContent = "11バック中";
        rBar.appendChild(p);
      }
      if (state.lockedSuits && state.lockedSuits.length) {
        const p = document.createElement("span");
        p.className = "status-pill alert";
        p.textContent = `しばり (${state.lockedSuits.join(",")})`;
        rBar.appendChild(p);
      }
    }

    // Opponents (relPos 1=West, 2=North, 3=East)
    for (const s of state.seats) {
      if (s.relPos === 0) continue;
      const box = document.getElementById(`seat-rel-${s.relPos}`);
      if (!box) continue;
      box.classList.toggle("active-turn", Boolean(s.isTurn));
      document.getElementById(`av-rel-${s.relPos}`).textContent = s.isHuman ? "P" : s.avatar;
      document.getElementById(`name-rel-${s.relPos}`).textContent = s.name;
      document.getElementById(`rank-rel-${s.relPos}`).textContent = s.rankTitle;
      const cntEl = document.getElementById(`cnt-rel-${s.relPos}`);
      if (s.finishRank > 0) {
        cntEl.textContent = `${s.finishRank}位上がり`;
        cntEl.classList.remove("danger");
      } else {
        cntEl.textContent = `残り ${s.cardCount}枚`;
        cntEl.classList.toggle("danger", s.cardCount > 0 && s.cardCount <= 2);
      }
    }

    // Center Played Cards
    const centerRow = document.getElementById("centerPlayedCards");
    const centerCap = document.getElementById("centerPlayCaption");
    centerRow.innerHTML = "";
    centerCap.style.display = "none";
    if (state.tablePlay && state.tablePlay.cards) {
      for (const c of state.tablePlay.cards) {
        centerRow.appendChild(createSimpleCard(c));
      }
      if (state.tablePlay.label) {
        centerCap.textContent = state.tablePlay.label;
        centerCap.style.display = "block";
      }
    }

    // Babanuki Draw Box
    const babaBox = document.getElementById("babaDrawBox");
    const isMyTurn = !state.isSpectator && state.phase === "playing" && state.currentTurn === state.mySeat;
    if (state.mode === "babanuki" && isMyTurn && state.babaTarget) {
      const t = state.babaTarget;
      document.getElementById("babaDrawPrompt").textContent =
        `${t.targetName}の手札 (${t.cardCount}枚) から1枚クリックして引いてください`;
      const list = document.getElementById("babaCardsList");
      list.innerHTML = "";
      for (let i = 0; i < t.cardCount; i++) {
        const b = document.createElement("div");
        b.className = "baba-back-card";
        b.textContent = "？";
        b.addEventListener("click", () => {
          babaBox.classList.remove("show");
          sendRoomAction({ action: "draw", cardIndex: i });
        });
        list.appendChild(b);
      }
      babaBox.classList.add("show");
    } else {
      babaBox.classList.remove("show");
    }

    // Bottom Player Panel
    renderMyBottomPanel(state);

    // Result Modal
    const resModal = document.getElementById("resultModal");
    if (state.phase === "round_end") {
      const list = document.getElementById("resultList");
      list.innerHTML = "";
      for (const st of state.standings || []) {
        const d = document.createElement("div");
        d.className = "rank-row-item";
        d.innerHTML = `<span>${st.place}位: ${st.name}</span><span style="color:#fde047">${st.rankTitle}</span>`;
        list.appendChild(d);
      }
      resModal.classList.add("show");
    } else {
      resModal.classList.remove("show");
    }
  }

  function renderMyBottomPanel(state) {
    const myInfo = state.seats.find(s => s.relPos === 0);
    const statusTxt = document.getElementById("myStatusText");
    const btnHint = document.getElementById("btnHint");
    const btnPlay = document.getElementById("btnPlay");
    const btnPass = document.getElementById("btnPass");
    const btnShuffle = document.getElementById("btnShuffle");
    const btnTakeSeat = document.getElementById("btnTakeSeat");
    const btnGoSpec = document.getElementById("btnGoSpectate");
    const handRow = document.getElementById("myHandRow");

    if (state.isSpectator) {
      statusTxt.textContent = `観戦中 (${playerName})`;
      btnHint.style.display = "none";
      btnPlay.style.display = "none";
      btnPass.style.display = "none";
      btnShuffle.style.display = "none";
      btnGoSpec.style.display = "none";
      btnTakeSeat.style.display = state.hasOpenBotSeat ? "inline-block" : "none";
      handRow.innerHTML = `<div style="color:#94a3b8;font-size:13px">現在観戦モードです。${state.hasOpenBotSeat ? "「空き席に座る」でいつでも参加できます。" : "4席満席のため観戦中です。"}</div>`;
      return;
    }

    btnTakeSeat.style.display = "none";
    btnGoSpec.style.display = "inline-block";
    statusTxt.textContent = `${playerName} (${myInfo ? myInfo.rankTitle : "平民"}) ・ 手札 ${state.myHand.length}枚`;

    if (state.mode === "babanuki") {
      btnHint.style.display = "none";
      btnPlay.style.display = "none";
      btnPass.style.display = "none";
      btnShuffle.style.display = "inline-block";
    } else {
      btnHint.style.display = "inline-block";
      btnPlay.style.display = "inline-block";
      btnPass.style.display = "inline-block";
      btnShuffle.style.display = "none";
    }

    const validIds = new Set(state.myHand.map(c => c.id));
    for (const id of [...selectedCardIds]) {
      if (!validIds.has(id)) selectedCardIds.delete(id);
    }

    const playableSet = new Set(state.playableIds || []);
    const isMyTurn = state.phase === "playing" && state.currentTurn === state.mySeat;

    handRow.innerHTML = "";
    for (const card of state.myHand) {
      const isSel = selectedCardIds.has(card.id);
      let cls = isSel ? "selected" : "";
      if (state.mode === "daifugo" && isMyTurn && !playableSet.has(card.id) && state.tablePlay) {
        cls += " unplayable";
      }
      const el = createSimpleCard(card, cls);
      el.addEventListener("click", () => {
        if (state.mode === "babanuki") return;
        if (!isMyTurn) return;
        if (selectedCardIds.has(card.id)) selectedCardIds.delete(card.id);
        else selectedCardIds.add(card.id);
        playSfx("select");
        renderMyBottomPanel(state);
      });
      handRow.appendChild(el);
    }

    if (state.mode === "daifugo") {
      btnHint.disabled = !isMyTurn;
      btnPass.disabled = !isMyTurn || !state.tablePlay;
      btnPlay.disabled = !isMyTurn || selectedCardIds.size === 0;
    }
  }

  window.selectSuggestedMove = function() {
    if (!roomState || !roomState.suggestedIds || !roomState.suggestedIds.length) {
      showToast("出せるカードがありません (パス推奨)");
      return;
    }
    selectedCardIds = new Set(roomState.suggestedIds);
    playSfx("select");
    renderMyBottomPanel(roomState);
  };

  window.commitPlayerMove = function() {
    if (!roomState || selectedCardIds.size === 0) return;
    const ids = [...selectedCardIds];
    selectedCardIds.clear();
    sendRoomAction({ action: "play", cardIds: ids });
  };

  window.playerPassTurn = function() {
    selectedCardIds.clear();
    sendRoomAction({ action: "pass" });
  };

  window.shufflePlayerHand = function() {
    sendRoomAction({ action: "shuffle" });
  };

  window.toggleRulesModal = function(show) {
    if (show && roomState && roomState.rules) {
      document.getElementById("ruleRev").checked = Boolean(roomState.rules.revolution);
      document.getElementById("rule8Giri").checked = Boolean(roomState.rules.eightGiri);
      document.getElementById("rule11Back").checked = Boolean(roomState.rules.elevenBack);
      document.getElementById("ruleShibari").checked = Boolean(roomState.rules.shibari);
      document.getElementById("ruleSpade3").checked = Boolean(roomState.rules.spade3);
      document.getElementById("ruleStairs").checked = Boolean(roomState.rules.stairs);
      document.getElementById("ruleMiyako").checked = Boolean(roomState.rules.miyakoOchi);
    }
    document.getElementById("rulesModal").classList.toggle("show", Boolean(show));
  };

  window.saveRulesAndClose = function() {
    sendRoomAction({
      action: "update_rules",
      rules: {
        revolution: document.getElementById("ruleRev").checked,
        eightGiri: document.getElementById("rule8Giri").checked,
        elevenBack: document.getElementById("rule11Back").checked,
        shibari: document.getElementById("ruleShibari").checked,
        spade3: document.getElementById("ruleSpade3").checked,
        stairs: document.getElementById("ruleStairs").checked,
        miyakoOchi: document.getElementById("ruleMiyako").checked,
      },
    });
    toggleRulesModal(false);
  };

  // Start on Room Selection Lobby Screen and poll live room counts
  refreshLobby();
  lobbyTimer = setInterval(refreshLobby, 2000);
})();
</script>
</body>
</html>
"""


@router.get("/trump", response_class=HTMLResponse)
@router.get("/trump/", response_class=HTMLResponse)
async def trump_game_page():
    _ensure_bg_loop()
    return HTMLResponse(content=_HTML_PAGE, headers={"Cache-Control": "no-store"})
