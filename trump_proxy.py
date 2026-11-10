"""Simple, Easy-to-Play Online 4-Player Trump (Lobby + 1 Room per Rule: Daifugo & Poker + Spectator Mode) for School Hub."""
from __future__ import annotations

import asyncio
from collections import Counter
import json
import random
import re
import secrets
import time
import unicodedata
import uuid
from typing import Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from openchat_proxy import (
    _CHAT_USERS,
    _clean_str,
    _hash_chat_password,
    _normalize_name,
    _save_chat_users,
    _sign_auth_token,
    _verify_auth_token,
    _verify_chat_password,
)

router = APIRouter()

SERVICE_NAME = "トランプ (大富豪・ポーカー)"
SERVICE_ICON = "🃏"
SERVICE_PATH = "/trump"

# ============================================================================
# Card & Rule Definitions (Server-Authoritative Engine + Strong AI + True Random)
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
    {"name": "レン (AI)", "style": "Master", "avatar": "R", "rate": 1500},
    {"name": "カイ (AI)", "style": "Power", "avatar": "K", "rate": 1520},
    {"name": "レイ (AI)", "style": "Tactics", "avatar": "L", "rate": 1550},
    {"name": "ミナ (AI)", "style": "Brain", "avatar": "M", "rate": 1580},
]

RANK_TITLES_DAIFUGO = ["大富豪", "富豪", "貧民", "大貧民"]
RANK_TITLES_POKER = ["1位 (勝者)", "2位", "3位", "4位"]


# ----------------------------------------------------------------------------
# Rating (レート) Helper Functions (Stored with OpenChat Accounts)
# ----------------------------------------------------------------------------
def get_user_rate(username: str) -> int:
    norm = _normalize_name(username)
    if not norm or norm not in _CHAT_USERS:
        return 1500
    return int(_CHAT_USERS[norm].get("trumpRate") or 1500)


def update_user_rate(username: str, delta: int, is_win: bool) -> int:
    norm = _normalize_name(username)
    if not norm or norm not in _CHAT_USERS:
        return 1500
    u = _CHAT_USERS[norm]
    curr = int(u.get("trumpRate") or 1500)
    new_rate = max(1000, curr + delta)
    u["trumpRate"] = new_rate
    u["trumpGames"] = int(u.get("trumpGames") or 0) + 1
    if is_win:
        u["trumpWins"] = int(u.get("trumpWins") or 0) + 1
    else:
        u["trumpLosses"] = int(u.get("trumpLosses") or 0) + 1
    _save_chat_users()
    return new_rate


# ----------------------------------------------------------------------------
# Deck Building with Cryptographic PRNG (完全ランダム)
# ----------------------------------------------------------------------------
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
    if mode == "daifugo":
        for j in range(2):
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
    # Complete, cryptographically secure uniform random shuffle
    secrets.SystemRandom().shuffle(deck)
    return deck


# ----------------------------------------------------------------------------
# 5-Card Poker Hand Evaluator
# ----------------------------------------------------------------------------
def eval_poker_5(hand: List[dict]) -> Tuple[int, str, List[int]]:
    """Evaluates a 5-card poker hand and returns (score, hand_name, tiebreaker_ranks).
    Score table:
      10: ロイヤルストレートフラッシュ
      9:  ストレートフラッシュ
      8:  フォーカード
      7:  フルハウス
      6:  フラッシュ
      5:  ストレート
      4:  スリーカード
      3:  ツーペア
      2:  ワンペア
      1:  ハイカード
    """
    if len(hand) != 5:
        return (0, "無効", [])
    suits = [c["suit"] for c in hand]
    ranks = [14 if c["num"] == 1 else int(c["num"]) for c in hand]
    ranks.sort(reverse=True)
    is_flush = (len(set(suits)) == 1)

    is_straight = False
    straight_high = 0
    unique_ranks = sorted(list(set(ranks)), reverse=True)
    if len(unique_ranks) == 5:
        if unique_ranks[0] - unique_ranks[4] == 4:
            is_straight = True
            straight_high = unique_ranks[0]
        elif unique_ranks == [14, 5, 4, 3, 2]:
            is_straight = True
            straight_high = 5  # Wheel straight (A-2-3-4-5)

    counts = Counter(ranks)
    freq = sorted(counts.items(), key=lambda x: (x[1], x[0]), reverse=True)
    pattern = [count for _, count in freq]
    order_ranks = [r for r, _ in freq]

    if is_flush and is_straight:
        if straight_high == 14:
            return (10, "ロイヤルストレートフラッシュ", [14])
        return (9, "ストレートフラッシュ", [straight_high])
    if pattern == [4, 1]:
        return (8, "フォーカード", order_ranks)
    if pattern == [3, 2]:
        return (7, "フルハウス", order_ranks)
    if is_flush:
        return (6, "フラッシュ", ranks)
    if is_straight:
        return (5, "ストレート", [straight_high])
    if pattern == [3, 1, 1]:
        return (4, "スリーカード", order_ranks)
    if pattern == [2, 2, 1]:
        return (3, "ツーペア", order_ranks)
    if pattern == [2, 1, 1, 1]:
        return (2, "ワンペア", order_ranks)
    return (1, "ハイカード", ranks)


# ============================================================================
# Trump Room Class (Daifugo & Poker)
# ============================================================================
class TrumpRoom:
    """Single shared online room for a specific game mode ('daifugo' or 'poker')."""

    def __init__(self, mode: str, title: str, description: str) -> None:
        self.mode = mode  # 'daifugo' | 'poker'
        self.title = title
        self.description = description
        self.lock = asyncio.Lock()
        self.clients: Dict[str, WebSocket] = {}
        self.participants: Dict[str, dict] = {}  # pid -> {id, name, last_seen, prefer_spectate, waiting_for_next}
        self.seats: List[Optional[str]] = [None, None, None, None]  # pid or None (AI Bot)
        self.seat_ranks: List[str] = ["平民", "平民", "平民", "平民"]
        self.hands: List[List[dict]] = [[], [], [], []]
        self.draw_pile: List[dict] = []
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

        # Poker-specific state
        self.poker_exchange_done: List[bool] = [False, False, False, False]
        self.poker_showdown_data: Optional[List[dict]] = None
        self.rate_changes: Dict[int, dict] = {}

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
        if self.mode == "poker":
            # Ace is 14 in poker sorting, descending
            h.sort(key=lambda c: (14 if c.get("num") == 1 else int(c.get("num") or 0), str(c.get("suit") or "")), reverse=True)
        else:
            h.sort(key=lambda c: (self.card_power(c), str(c.get("suit") or "")))

    def seat_display_name(self, seat: int) -> str:
        pid = self.seats[seat]
        if pid and pid in self.participants:
            return str(self.participants[pid].get("name") or f"プレイヤー{seat + 1}")
        return BOT_PROFILES[seat]["name"]

    def seat_rate(self, seat: int) -> int:
        pid = self.seats[seat]
        if pid and pid in self.participants:
            return get_user_rate(self.participants[pid].get("name", ""))
        return BOT_PROFILES[seat].get("rate", 1500)

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
        is_playing = (self.phase != "round_end")

        if p is None:
            clean_name = (name or "").strip()[:16] or f"ゲスト{secrets.SystemRandom().randint(100, 999)}"
            p = {
                "id": pid,
                "name": clean_name,
                "last_seen": now,
                "prefer_spectate": bool(prefer_spectate) if prefer_spectate is not None else False,
                "waiting_for_next": False,
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

        # --------------------------------------------------------------------
        # 試合が終わるまで新規の人入れないように (Lock during active matches)
        # --------------------------------------------------------------------
        if is_playing:
            # Match is currently in progress! Do not allow joining seats mid-game.
            p["prefer_spectate"] = True
            p["waiting_for_next"] = True
            return

        # If round has ended, player can sit down into an open bot seat
        for s in range(4):
            if self.seats[s] is None or self.seats[s] not in self.participants:
                self.seats[s] = pid
                p["waiting_for_next"] = False
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

        # Only seat non-spectators if round is in round_end
        if self.phase == "round_end":
            for pid, p in list(self.participants.items()):
                if not p.get("prefer_spectate") and pid not in self.seats:
                    for s in range(4):
                        if self.seats[s] is None:
                            self.seats[s] = pid
                            p["waiting_for_next"] = False
                            changed = True
                            break
        return changed

    def get_summary(self) -> dict:
        self.prune_stale_participants()
        human_seats = sum(1 for s in range(4) if self.is_seat_human(s))
        spec_count = sum(1 for k in self.participants if k not in self.seats)
        seated_info = [
            {"name": self.seat_display_name(s), "rate": self.seat_rate(s), "isHuman": self.is_seat_human(s)}
            for s in range(4)
        ]
        return {
            "mode": self.mode,
            "title": self.title,
            "description": self.description,
            "humanCount": human_seats,
            "maxSeats": 4,
            "spectatorCount": spec_count,
            "isFull": human_seats >= 4,
            "roundNumber": self.round_number,
            "isMatchInProgress": (self.phase != "round_end"),
            "seatedInfo": seated_info,
        }

    # ------------------------------------------------------------------------
    # Round Initialization & Dealing (配るモーション対応)
    # ------------------------------------------------------------------------
    def _start_new_round(self, reset_ranks: bool = False) -> None:
        # Seat any waiting participants before starting new game
        for pid, p in list(self.participants.items()):
            if p.get("waiting_for_next") and pid not in self.seats:
                for s in range(4):
                    if self.seats[s] is None or self.seats[s] not in self.participants:
                        self.seats[s] = pid
                        p["prefer_spectate"] = False
                        p["waiting_for_next"] = False
                        self._emit_event("bubble", f"{p['name']}さんが試合に参加しました", seat=s, sfx="select")
                        break

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
        self.rate_changes = {}

        # Completely random deck shuffle using OS CSPRNG
        deck = _build_deck(self.mode)

        if self.mode == "daifugo":
            self.hands = [[], [], [], []]
            for idx, card in enumerate(deck):
                self.hands[idx % 4].append(card)
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
        elif self.mode == "poker":
            # 5 cards dealt to each of 4 players, 32 remaining in draw_pile
            self.hands = [deck[0:5], deck[5:10], deck[10:15], deck[15:20]]
            self.draw_pile = deck[20:]
            for s in range(4):
                self.sort_hand(s)
            self.poker_exchange_done = [False, False, False, False]
            self.poker_showdown_data = None
            self.current_turn = 0

        self.turn_since = time.time()
        self._emit_event("sfx", "カードが配られました", seat=-1, sfx="card")

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
    # Daifugo Rule & Move Logic
    # ------------------------------------------------------------------------
    def analyze_combo(self, cards: List[dict]) -> Optional[dict]:
        if not cards:
            return None
        n = len(cards)
        jokers = [c for c in cards if c.get("isJoker")]
        normals = [c for c in cards if not c.get("isJoker")]
        j_count = len(jokers)

        if n == 1:
            c = cards[0]
            pwr = self.card_power(c)
            return {
                "type": "single",
                "count": 1,
                "power": pwr,
                "cards": cards,
                "suits": [c["suit"]],
                "containsEight": (c.get("num") == 8),
                "containsEleven": (c.get("num") == 11),
                "isSpade3": (c.get("suit") == "S" and c.get("num") == 3),
            }

        if normals:
            ref_num = normals[0]["num"]
            if all(c["num"] == ref_num for c in normals):
                ref_power = self.card_power(normals[0])
                c_types = {2: "pair", 3: "triple", 4: "quad"}
                ctype = c_types.get(n, "pair")
                has_8 = any(c["num"] == 8 for c in normals)
                has_11 = any(c["num"] == 11 for c in normals)
                return {
                    "type": ctype,
                    "count": n,
                    "power": ref_power,
                    "cards": cards,
                    "suits": [c["suit"] for c in normals],
                    "containsEight": has_8,
                    "containsEleven": has_11,
                    "isSpade3": False,
                }
        return None

    def _suits_match_lock(self, table_suits: List[str], move_suits: List[str]) -> bool:
        if not table_suits or not move_suits:
            return False
        return set(table_suits) == set(move_suits)

    def is_legal_play(self, combo: Optional[dict]) -> bool:
        if combo is None:
            return False
        if self.table_play is None:
            return True

        t_type = self.table_play["type"]
        t_count = self.table_play["count"]
        t_power = self.table_play["power"]

        if self.rules.get("spade3") and t_type == "single" and self.table_play.get("isSingleJoker") and combo.get("isSpade3"):
            return True

        if combo["type"] != t_type or combo["count"] != t_count:
            return False

        if self.rules.get("shibari") and self.locked_suits:
            if not self._suits_match_lock(self.locked_suits, combo["suits"]):
                return False

        return combo["power"] > t_power

    def generate_legal_combos(self, seat: int) -> List[dict]:
        hand = self.hands[seat]
        legal: List[dict] = []
        for c in hand:
            cb = self.analyze_combo([c])
            if self.is_legal_play(cb):
                legal.append(cb)
        for i in range(len(hand)):
            for j in range(i + 1, len(hand)):
                cb = self.analyze_combo([hand[i], hand[j]])
                if self.is_legal_play(cb):
                    legal.append(cb)
        for i in range(len(hand)):
            for j in range(i + 1, len(hand)):
                for k in range(j + 1, len(hand)):
                    cb = self.analyze_combo([hand[i], hand[j], hand[k]])
                    if self.is_legal_play(cb):
                        legal.append(cb)
        return legal

    def choose_best_ai_move(self, seat: int) -> Optional[dict]:
        legal = self.generate_legal_combos(seat)
        if not legal:
            return None
        legal.sort(key=lambda m: m["power"])
        return legal[0]

    def execute_daifugo_play(self, seat: int, combo: dict) -> None:
        played_ids = {c["id"] for c in combo["cards"]}
        self.hands[seat] = [c for c in self.hands[seat] if c["id"] not in played_ids]

        self.prev_table_play = self.table_play
        is_single_joker = (combo["count"] == 1 and bool(combo["cards"][0].get("isJoker")))
        self.table_play = {
            "seat": seat,
            "type": combo["type"],
            "count": combo["count"],
            "power": combo["power"],
            "cards": combo["cards"],
            "suits": combo["suits"],
            "isSingleJoker": is_single_joker,
        }
        self.last_play_seat = seat
        self.passed_seats.discard(seat)

        if self.rules.get("shibari") and self.prev_table_play:
            if self._suits_match_lock(self.prev_table_play.get("suits", []), combo["suits"]):
                if not self.locked_suits:
                    self.locked_suits = list(combo["suits"])
                    self._emit_event("cutin", "しばり発動！", seat=seat, sfx="special")

        if self.rules.get("revolution") and combo["count"] >= 4:
            self.is_revolution = not self.is_revolution
            txt = "革命発動！" if self.is_revolution else "革命返し！"
            self._emit_event("cutin", txt, seat=seat, sfx="revolution")

        if self.rules.get("elevenBack") and combo.get("containsEleven"):
            self.is_eleven_back = not self.is_eleven_back
            txt = "11バック発動！" if self.is_eleven_back else "11バック解除！"
            self._emit_event("cutin", txt, seat=seat, sfx="special")

        is_eight_giri = self.rules.get("eightGiri") and combo.get("containsEight")
        self._emit_event("sfx", "", seat=seat, sfx="card")

        if len(self.hands[seat]) == 0 and seat not in self.finish_order:
            self.finish_order.append(seat)
            rank_idx = len(self.finish_order)
            self._emit_event("bubble", f"{rank_idx}位上がり！", seat=seat, sfx="special")

        active = [s for s in range(4) if len(self.hands[s]) > 0]
        if len(active) <= 1:
            for rem in active:
                if rem not in self.finish_order:
                    self.finish_order.append(rem)
            self._conclude_round()
            return

        if is_eight_giri:
            self._emit_event("cutin", "8切り！ ターン継続", seat=seat, sfx="special")
            self._clear_trick()
            if len(self.hands[seat]) > 0:
                self.current_turn = seat
            else:
                self.current_turn = self._next_active_seat(seat)
            self.turn_since = time.time()
            return

        self._advance_daifugo_turn(seat)

    def execute_daifugo_pass(self, seat: int) -> None:
        self.passed_seats.add(seat)
        self._emit_event("bubble", "パス", seat=seat, sfx="pass")
        self._advance_daifugo_turn(seat)

    def _clear_trick(self) -> None:
        self.table_play = None
        self.prev_table_play = None
        self.locked_suits = None
        self.passed_seats.clear()
        if self.is_eleven_back:
            self.is_eleven_back = False

    def _next_active_seat(self, from_seat: int) -> int:
        for step in range(1, 5):
            cand = (from_seat + step) % 4
            if len(self.hands[cand]) > 0 and cand not in self.finish_order:
                return cand
        return 0

    def _advance_daifugo_turn(self, from_seat: int) -> None:
        active_remaining = [s for s in range(4) if len(self.hands[s]) > 0 and s not in self.finish_order]
        if len(active_remaining) <= 1:
            for rem in active_remaining:
                if rem not in self.finish_order:
                    self.finish_order.append(rem)
            self._conclude_round()
            return

        next_s = None
        for step in range(1, 4):
            cand = (from_seat + step) % 4
            if cand in active_remaining and cand not in self.passed_seats:
                next_s = cand
                break

        if next_s is None:
            self._clear_trick()
            next_s = self._next_active_seat(self.last_play_seat if self.last_play_seat is not None else from_seat)

        self.current_turn = next_s
        self.turn_since = time.time()

    # ------------------------------------------------------------------------
    # Poker Execution & Smart AI
    # ------------------------------------------------------------------------
    def choose_poker_ai_exchange(self, seat: int) -> List[int]:
        """Smart AI card exchange decision for 5-card poker."""
        hand = self.hands[seat]
        if len(hand) != 5:
            return []
        score, _, _ = eval_poker_5(hand)
        # Straight or higher (Flush, Full House, 4 of a kind, Straight Flush): keep all 5 cards!
        if score >= 5:
            return []
        ranks = [14 if c["num"] == 1 else int(c["num"]) for c in hand]
        counts = Counter(ranks)
        if score == 4:  # Three of a kind: keep the 3 matching cards, exchange 2 kickers
            trip_rank = [r for r, cnt in counts.items() if cnt == 3][0]
            return [i for i, c in enumerate(hand) if (14 if c["num"] == 1 else int(c["num"])) != trip_rank]
        if score == 3:  # Two pair: keep the pairs, exchange 1 kicker
            pair_ranks = [r for r, cnt in counts.items() if cnt == 2]
            return [i for i, c in enumerate(hand) if (14 if c["num"] == 1 else int(c["num"])) not in pair_ranks]
        if score == 2:  # One pair: keep the pair, exchange 3 kickers
            pair_rank = [r for r, cnt in counts.items() if cnt == 2][0]
            return [i for i, c in enumerate(hand) if (14 if c["num"] == 1 else int(c["num"])) != pair_rank]
        # High Card (score == 1): check 4 to a flush
        suits = [c["suit"] for c in hand]
        s_counts = Counter(suits)
        for s, cnt in s_counts.items():
            if cnt == 4:
                return [i for i, c in enumerate(hand) if c["suit"] != s]
        # Keep highest 1 or 2 cards, exchange rest
        sorted_indices = sorted(range(5), key=lambda i: ranks[i], reverse=True)
        return sorted_indices[1:]

    def execute_poker_exchange(self, seat: int, card_indices: List[int]) -> None:
        if self.mode != "poker" or self.phase != "playing":
            return
        valid_indices = sorted(list({int(i) for i in card_indices if 0 <= int(i) < len(self.hands[seat])}), reverse=True)
        exchanged_count = len(valid_indices)
        if exchanged_count > 0:
            for idx in valid_indices:
                if idx < len(self.hands[seat]):
                    self.hands[seat].pop(idx)
            while len(self.hands[seat]) < 5 and self.draw_pile:
                self.hands[seat].append(self.draw_pile.pop(0))
            self.sort_hand(seat)
            self._emit_event("bubble", f"{exchanged_count}枚交換！", seat=seat, sfx="card")
        else:
            self._emit_event("bubble", "チェンジなし (勝負)", seat=seat, sfx="select")

        self.poker_exchange_done[seat] = True

        # Find next player to exchange
        next_seat = None
        for step in range(1, 5):
            cand = (seat + step) % 4
            if not self.poker_exchange_done[cand]:
                next_seat = cand
                break

        if next_seat is not None:
            self.current_turn = next_seat
            self.turn_since = time.time()
        else:
            # All 4 players done exchanging! Showdown!
            self.execute_poker_showdown()

    def execute_poker_showdown(self) -> None:
        """Evaluate all hands, rank 1st to 4th, update rates, and conclude round."""
        evals = [eval_poker_5(self.hands[s]) for s in range(4)]
        ranked = sorted(range(4), key=lambda s: (evals[s][0], evals[s][2]), reverse=True)
        self.finish_order = ranked

        self.poker_showdown_data = []
        for rank_idx, s in enumerate(ranked):
            score, hand_name, _ = evals[s]
            self.seat_ranks[s] = hand_name
            self.poker_showdown_data.append({
                "place": rank_idx + 1,
                "seat": s,
                "name": self.seat_display_name(s),
                "handName": hand_name,
                "score": score,
                "cards": list(self.hands[s]),
                "rate": self.seat_rate(s),
            })

        self.table_play = {
            "seat": ranked[0],
            "cards": list(self.hands[ranked[0]]),
            "label": f"勝者: {self.seat_display_name(ranked[0])}【{evals[ranked[0]][1]}】",
        }

        # Rate adjustments: 1st: +20, 2nd: +8, 3rd: -8, 4th: -16
        rate_deltas = {ranked[0]: 20, ranked[1]: 8, ranked[2]: -8, ranked[3]: -16}
        self.rate_changes = {}
        for s in range(4):
            delta = rate_deltas.get(s, 0)
            pid = self.seats[s]
            if pid and pid in self.participants:
                name = self.participants[pid].get("name", "")
                new_rate = update_user_rate(name, delta, is_win=(s == ranked[0]))
                self.rate_changes[s] = {"delta": delta, "newRate": new_rate}

        self.phase = "round_end"
        self.phase_since = time.time()
        winner_name = self.seat_display_name(ranked[0])
        winner_hand = evals[ranked[0]][1]
        self._emit_event("cutin", f"勝負あり！ 1位: {winner_name} ({winner_hand})", sfx="special")

    # ------------------------------------------------------------------------
    # Round Conclusion & Rate Update
    # ------------------------------------------------------------------------
    def _conclude_round(self) -> None:
        titles = RANK_TITLES_DAIFUGO if self.mode == "daifugo" else RANK_TITLES_POKER
        for idx, s in enumerate(self.finish_order[:4]):
            self.seat_ranks[s] = titles[idx] if idx < len(titles) else "平民"

        # Update rates for Daifugo
        rate_deltas = {self.finish_order[0]: 20, self.finish_order[1]: 8, self.finish_order[2]: -8, self.finish_order[3]: -16}
        self.rate_changes = {}
        for s in range(4):
            delta = rate_deltas.get(s, 0)
            pid = self.seats[s]
            if pid and pid in self.participants:
                name = self.participants[pid].get("name", "")
                new_rate = update_user_rate(name, delta, is_win=(s == self.finish_order[0]))
                self.rate_changes[s] = {"delta": delta, "newRate": new_rate}

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
        is_spectator = (my_seat is None)
        perspective_seat = my_seat if my_seat is not None else 0

        human_seats = sum(1 for s in range(4) if self.is_seat_human(s))
        spectator_list = [p["name"] for k, p in self.participants.items() if k not in self.seats]
        p_obj = self.participants.get(pid, {})
        waiting_for_next = bool(p_obj.get("waiting_for_next"))
        is_playing = (self.phase != "round_end")

        seats_info = []
        for s in range(4):
            rel_pos = (s - perspective_seat) % 4  # 0=Bottom(You), 1=Left, 2=Top, 3=Right
            fin_idx = self.finish_order.index(s) + 1 if s in self.finish_order else 0
            showdown_cards = None
            if self.mode == "poker" and self.phase == "round_end":
                showdown_cards = self.hands[s]

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
                "rate": self.seat_rate(s),
                "showdownCards": showdown_cards,
                "exchangeDone": self.poker_exchange_done[s] if self.mode == "poker" else False,
            })

        my_hand = self.hands[perspective_seat] if not is_spectator else []

        playable_ids: List[str] = []
        if not is_spectator and self.phase == "playing" and self.current_turn == my_seat and self.mode == "daifugo":
            legal = self.generate_legal_combos(my_seat)
            playable_ids = list({c["id"] for m in legal for c in m["cards"]})

        standings = []
        if self.phase == "round_end":
            for idx, s in enumerate(self.finish_order[:4]):
                delta_info = self.rate_changes.get(s, {})
                standings.append({
                    "place": idx + 1,
                    "seat": s,
                    "name": self.seat_display_name(s),
                    "rankTitle": self.seat_ranks[s],
                    "isMe": (s == my_seat),
                    "rate": self.seat_rate(s),
                    "rateDelta": delta_info.get("delta", 0),
                    "newRate": delta_info.get("newRate", self.seat_rate(s)),
                })

        return {
            "mode": self.mode,
            "roomTitle": self.title,
            "roundNumber": self.round_number,
            "phase": self.phase,
            "isMatchInProgress": is_playing,
            "waitingForNextGame": waiting_for_next,
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
            "tablePlay": self.table_play,
            "prevTablePlay": self.prev_table_play,
            "pokerShowdown": self.poker_showdown_data,
            "standings": standings,
            "events": self.recent_events[-8:],
        }


# ============================================================================
# Singleton Rooms (Daifugo & Poker)
# ============================================================================
ROOMS: Dict[str, TrumpRoom] = {
    "daifugo": TrumpRoom(
        "daifugo",
        "大富豪ルーム",
        "革命・8切り・11バック・しばり・スペ3返し対応（4人対戦＋観戦・レート対応）",
    ),
    "poker": TrumpRoom(
        "poker",
        "ポーカールーム",
        "本格5枚ドローポーカー！カード交換と心理戦で最強の役を目指せ（4人対戦＋観戦・レート対応）",
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
                    if now - room.phase_since >= 7.0:
                        room.round_number += 1
                        room._start_new_round(reset_ranks=False)
                        should_broadcast = True
                elif room.phase == "playing":
                    turn_seat = room.current_turn
                    is_human_turn = room.is_seat_human(turn_seat)
                    elapsed = now - room.turn_since
                    if (not is_human_turn and elapsed >= 1.0) or (is_human_turn and elapsed >= 25.0):
                        if room.mode == "daifugo":
                            mv = room.choose_best_ai_move(turn_seat)
                            if mv:
                                room.execute_daifugo_play(turn_seat, mv)
                            else:
                                if room.table_play is not None:
                                    room.execute_daifugo_pass(turn_seat)
                            should_broadcast = True
                        elif room.mode == "poker":
                            if not room.poker_exchange_done[turn_seat]:
                                if is_human_turn:
                                    room.execute_poker_exchange(turn_seat, [])
                                else:
                                    discards = room.choose_poker_ai_exchange(turn_seat)
                                    room.execute_poker_exchange(turn_seat, discards)
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
    m = (mode or "").lower().strip()
    return ROOMS["poker"] if m == "poker" else ROOMS["daifugo"]


# ============================================================================
# API Routes (Auth Integration with OpenChat + Lobby + State + Actions)
# ============================================================================
@router.get("/trump/api/lobby")
async def trump_lobby_info():
    _ensure_bg_loop()
    rooms_data = []
    for key in ("daifugo", "poker"):
        r = ROOMS[key]
        async with r.lock:
            rooms_data.append(r.get_summary())
    return JSONResponse({"rooms": rooms_data}, headers={"Cache-Control": "no-store"})


@router.post("/trump/api/auth/login")
async def trump_auth_login(request: Request):
    """Unified OpenChat & Trump authentication (Shared PBKDF2 Password + Rate)."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    username = _clean_str(data.get("username") or data.get("name") or "", 24)
    password = _clean_str(data.get("password") or data.get("pass") or "", 64)
    cid = _clean_str(data.get("clientId") or "", 64)

    username = unicodedata.normalize("NFKC", str(username or "")).strip()
    username = re.sub(r"[\s\u200b-\u200d\ufeff]+", " ", username).strip()
    password = str(password or "").strip()

    if not username:
        return JSONResponse({"ok": False, "error": "名前を入力してください"}, status_code=400)
    if len(username) > 20:
        return JSONResponse({"ok": False, "error": "名前は20文字以内で入力してください"}, status_code=400)
    if not password:
        return JSONResponse({"ok": False, "error": "パスワードを入力してください"}, status_code=400)
    if len(password) < 4:
        return JSONResponse({"ok": False, "error": "パスワードは4文字以上で入力してください"}, status_code=400)

    norm = _normalize_name(username)
    if not norm:
        return JSONResponse({"ok": False, "error": "有効な名前を入力してください"}, status_code=400)
    if norm in ("SYS", "SYSTEM", "ADMIN", "ADMINISTRATOR", "MODERATOR", "GOOGLE", "SCHOOLHUB"):
        return JSONResponse({"ok": False, "error": "その名前は予約されており使用できません"}, status_code=400)

    # Check against shared OpenChat accounts database
    stored = _CHAT_USERS.get(norm)
    if stored:
        salt = str(stored.get("salt") or "")
        exp_hash = str(stored.get("hash") or "")
        if not _verify_chat_password(password, salt, exp_hash):
            return JSONResponse({"ok": False, "error": "パスワードが一致しません。正しいパスワードを入力してください。"}, status_code=401)
        stored["last_login"] = time.time()
        if cid:
            stored["clientId"] = cid
    else:
        # Register new user in shared account system
        salt = secrets.token_hex(16)
        h = _hash_chat_password(password, salt)
        stored = {
            "username": username,
            "salt": salt,
            "hash": h,
            "created_at": time.time(),
            "last_login": time.time(),
            "clientId": cid,
            "trumpRate": 1500,
            "trumpWins": 0,
            "trumpLosses": 0,
            "trumpGames": 0,
        }
        _CHAT_USERS[norm] = stored

    _save_chat_users()
    token = _sign_auth_token(username)
    rate = int(stored.get("trumpRate") or 1500)
    return JSONResponse({
        "ok": True,
        "username": username,
        "token": token,
        "rate": rate,
        "wins": int(stored.get("trumpWins") or 0),
        "games": int(stored.get("trumpGames") or 0),
    })


@router.get("/trump/api/auth/me")
async def trump_auth_me(request: Request):
    """Retrieve authenticated user profile and current rate."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        token = request.query_params.get("token", "").strip()
    verified = _verify_auth_token(token) if token else None
    if not verified:
        return JSONResponse({"ok": False, "error": "未ログイン"}, status_code=401)
    username = verified.get("username", "")
    norm = _normalize_name(username)
    stored = _CHAT_USERS.get(norm, {})
    return JSONResponse({
        "ok": True,
        "username": username,
        "rate": int(stored.get("trumpRate") or 1500),
        "wins": int(stored.get("trumpWins") or 0),
        "games": int(stored.get("trumpGames") or 0),
    })


@router.get("/trump/api/state/{mode}")
async def trump_get_state(mode: str, request: Request):
    _ensure_bg_loop()
    room = _get_room(mode)
    pid = (request.query_params.get("pid") or "").strip() or ("p_" + uuid.uuid4().hex[:12])
    name = (request.query_params.get("name") or "").strip()
    token = (request.query_params.get("token") or "").strip()
    if token:
        v = _verify_auth_token(token)
        if v:
            name = v.get("username", name)

    async with room.lock:
        room.touch_participant(pid, name=name if name else None)
        view = room.build_view_for(pid)
    return JSONResponse(view, headers={"Cache-Control": "no-store"})


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
    token = str(body.get("token") or "").strip()
    if token:
        v = _verify_auth_token(token)
        if v:
            name = v.get("username", name)

    act = str(body.get("action") or "").strip()

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
        if not want_spec and room.phase != "round_end":
            # 試合が終わるまで新規の人入れないように: Match in progress
            room._emit_event("bubble", "試合進行中のため終了まで参加できません", seat=-1)
            return
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

    elif act == "poker_exchange" and room.mode == "poker" and room.phase == "playing" and my_seat == room.current_turn:
        card_indices = body.get("cardIndices") or []
        room.execute_poker_exchange(my_seat, card_indices)

    elif act == "poker_stay" and room.mode == "poker" and room.phase == "playing" and my_seat == room.current_turn:
        room.execute_poker_exchange(my_seat, [])

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
    token = (websocket.query_params.get("token") or "").strip()
    if token:
        v = _verify_auth_token(token)
        if v:
            name = v.get("username", name)

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
# HTML UI (Lobby + Table + Deal Motion + Rate Display + Shared Auth Modal)
# ============================================================================
TRUMP_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<title>School Hub トランプ (大富豪・ポーカー)</title>
<link rel="icon" href="/favicon.ico">
<style>
:root {
  --felt-bg: radial-gradient(circle at 50% 45%, #14532d 0%, #064e3b 50%, #022c22 100%);
  --table-border: #15803d;
  --panel-bg: rgba(15, 23, 42, 0.94);
  --card-w: 66px;
  --card-h: 96px;
  --primary: #38bdf8;
  --accent: #f59e0b;
  --m3-easing: cubic-bezier(0.16, 1, 0.3, 1);
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body {
  margin: 0; padding: 0; width: 100%; height: 100%;
  background: #090d16; color: #f8fafc;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  overflow: hidden; user-select: none;
}
.app-container {
  display: flex; flex-direction: column; width: 100vw; height: 100dvh; position: relative;
}
/* Top App Bar */
.top-nav {
  height: 52px; background: rgba(10, 15, 29, 0.96); border-bottom: 1px solid rgba(255,255,255,0.08);
  display: flex; align-items: center; justify-content: space-between; padding: 0 16px; z-index: 50; flex-shrink: 0;
}
.brand { display: flex; align-items: center; gap: 8px; font-weight: 800; font-size: 15px; color: #38bdf8; }
.brand a { color: inherit; text-decoration: none; display: flex; align-items: center; gap: 6px; }
.nav-actions { display: flex; align-items: center; gap: 10px; }
.user-chip {
  display: flex; align-items: center; gap: 8px; background: rgba(255,255,255,0.06);
  padding: 4px 12px; border-radius: 9999px; font-size: 13px; font-weight: 600; border: 1px solid rgba(255,255,255,0.1);
  cursor: pointer; transition: background 0.2s;
}
.user-chip:hover { background: rgba(255,255,255,0.12); }
.rate-badge {
  background: linear-gradient(135deg, #f59e0b, #d97706); color: #000; font-weight: 800;
  padding: 2px 7px; border-radius: 9999px; font-size: 11px; letter-spacing: 0.3px;
}
.btn-nav {
  background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.14); color: #cbd5e1;
  padding: 6px 12px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; transition: all 0.2s;
}
.btn-nav:hover { background: rgba(255,255,255,0.16); color: #fff; }

/* 1. LOBBY SCREEN */
.lobby-screen {
  flex: 1; overflow-y: auto; padding: 24px 16px 40px; display: flex; flex-direction: column; align-items: center;
  background: radial-gradient(circle at 50% 20%, #1e293b 0%, #090d16 80%);
}
.lobby-container { width: 100%; max-width: 820px; }
.lobby-hero { text-align: center; margin-bottom: 24px; }
.lobby-hero h1 { font-size: 26px; font-weight: 900; margin: 0 0 8px; color: #f8fafc; }
.lobby-hero p { color: #94a3b8; font-size: 14px; margin: 0; line-height: 1.5; }

.auth-callout {
  background: rgba(56, 189, 248, 0.08); border: 1px solid rgba(56, 189, 248, 0.25);
  border-radius: 12px; padding: 12px 16px; margin-bottom: 24px; display: flex; align-items: center; justify-content: space-between;
  gap: 12px; font-size: 13px; color: #bae6fd;
}
.btn-primary-small {
  background: #0284c7; color: #fff; border: none; padding: 6px 14px; border-radius: 9999px; font-weight: 700;
  font-size: 12px; cursor: pointer; white-space: nowrap; transition: background 0.2s;
}
.btn-primary-small:hover { background: #0369a1; }

.room-cards-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 18px; }
.room-card {
  background: rgba(30, 41, 59, 0.7); border: 1px solid rgba(255,255,255,0.1); border-radius: 16px;
  padding: 20px; display: flex; flex-direction: column; justify-content: space-between; gap: 16px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.3); transition: transform 0.2s, border-color 0.2s;
}
.room-card:hover { transform: translateY(-3px); border-color: rgba(56, 189, 248, 0.4); }
.room-card-title { font-size: 18px; font-weight: 800; color: #f8fafc; display: flex; align-items: center; justify-content: space-between; }
.room-card-desc { font-size: 13px; color: #94a3b8; line-height: 1.5; margin: 4px 0 10px; }
.room-status-badge {
  font-size: 11px; padding: 3px 8px; border-radius: 9999px; font-weight: 700;
}
.badge-playing { background: rgba(245, 158, 11, 0.2); color: #f59e0b; border: 1px solid rgba(245, 158, 11, 0.3); }
.badge-open { background: rgba(34, 197, 94, 0.2); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.3); }

.room-players-list {
  display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; font-size: 12px; color: #cbd5e1;
}
.player-tag {
  background: rgba(255,255,255,0.06); padding: 3px 8px; border-radius: 6px; display: inline-flex; align-items: center; gap: 4px;
}
.room-actions { display: flex; gap: 10px; }
.btn-join-room {
  flex: 2; background: linear-gradient(135deg, #0284c7, #0369a1); color: #fff; border: none;
  padding: 10px 16px; border-radius: 10px; font-weight: 800; font-size: 14px; cursor: pointer; transition: all 0.2s;
}
.btn-join-room:hover { filter: brightness(1.1); transform: translateY(-1px); }
.btn-spec-room {
  flex: 1; background: rgba(255,255,255,0.08); color: #cbd5e1; border: 1px solid rgba(255,255,255,0.15);
  padding: 10px 14px; border-radius: 10px; font-weight: 700; font-size: 13px; cursor: pointer; transition: all 0.2s;
}
.btn-spec-room:hover { background: rgba(255,255,255,0.16); color: #fff; }

/* 2. GAME TABLE SCREEN */
.game-screen { flex: 1; display: flex; flex-direction: column; position: relative; background: var(--felt-bg); }
.table-board {
  flex: 1; position: relative; width: 100%; height: 100%; overflow: hidden;
  box-shadow: inset 0 0 100px rgba(0,0,0,0.6);
}

/* Match In Progress Lock Banner */
.match-lock-banner {
  position: absolute; top: 12px; left: 50%; transform: translateX(-50%);
  background: rgba(234, 88, 12, 0.92); color: #fff; padding: 6px 16px; border-radius: 9999px;
  font-size: 12px; font-weight: 800; z-index: 30; box-shadow: 0 4px 12px rgba(0,0,0,0.4);
  display: flex; align-items: center; gap: 6px; letter-spacing: 0.3px;
}

/* Opponent Seats */
.opp-seat {
  position: absolute; display: flex; align-items: center; gap: 10px;
  background: rgba(15, 23, 42, 0.85); backdrop-filter: blur(8px);
  padding: 6px 12px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.12);
  color: #fff; z-index: 20; transition: border-color 0.2s, box-shadow 0.2s;
}
.opp-seat.active-turn {
  border-color: #f59e0b; box-shadow: 0 0 16px rgba(245, 158, 11, 0.5);
}
.opp-north { top: 12px; left: 50%; transform: translateX(-50%); }
.opp-west { left: 12px; top: 38%; transform: translateY(-50%); flex-direction: column; text-align: center; }
.opp-east { right: 12px; top: 38%; transform: translateY(-50%); flex-direction: column; text-align: center; }

.opp-avatar {
  width: 38px; height: 38px; border-radius: 50%; background: #334155; display: flex;
  align-items: center; justify-content: center; font-weight: 800; font-size: 15px; border: 2px solid #64748b;
}
.opp-info { display: flex; flex-direction: column; font-size: 12px; }
.opp-name-line { display: flex; align-items: center; gap: 6px; font-weight: 700; }
.opp-rate-tag { font-size: 11px; color: #f59e0b; font-weight: 800; }
.opp-rank-tag { font-size: 10px; background: rgba(255,255,255,0.12); padding: 1px 5px; border-radius: 4px; }
.opp-cards-count { font-size: 11px; color: #94a3b8; font-weight: 600; margin-top: 2px; }

/* Opponent Showdown Hand Preview (in Poker showdown) */
.opp-showdown-cards { display: flex; gap: 3px; margin-top: 4px; }

/* Center Play Area */
.center-field {
  position: absolute; top: 48%; left: 50%; transform: translate(-50%, -50%);
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  z-index: 10; pointer-events: none; width: 90%; max-width: 640px;
}
.played-cards-row { display: flex; gap: 6px; justify-content: center; flex-wrap: wrap; }
.play-caption {
  margin-top: 10px; font-size: 14px; font-weight: 800; color: #fde047; text-shadow: 0 2px 4px rgba(0,0,0,0.8);
  background: rgba(0,0,0,0.6); padding: 4px 14px; border-radius: 9999px;
}
.poker-status-prompt {
  font-size: 14px; font-weight: 800; color: #38bdf8; background: rgba(15, 23, 42, 0.88);
  padding: 8px 18px; border-radius: 9999px; border: 1px solid rgba(56, 189, 248, 0.3);
  box-shadow: 0 4px 16px rgba(0,0,0,0.4); text-align: center;
}

/* CARDS STYLING & DEALING MOTION (配るモーション) */
@keyframes m3DealCard {
  0% {
    opacity: 0;
    transform: translateY(-220px) scale(0.3) rotate(25deg);
  }
  65% {
    opacity: 1;
    transform: translateY(-6px) scale(1.04) rotate(-2deg);
  }
  100% {
    opacity: 1;
    transform: translate(0, 0) scale(1) rotate(0deg);
  }
}
.card-dealing {
  animation: m3DealCard 0.42s var(--m3-easing) both;
}

.t-card {
  width: var(--card-w); height: var(--card-h); border-radius: 8px; background: #ffffff;
  border: 1px solid #cbd5e1; box-shadow: 0 4px 12px rgba(0,0,0,0.35); position: relative;
  display: flex; flex-direction: column; justify-content: space-between; padding: 4px 6px;
  cursor: pointer; transition: transform 0.18s, box-shadow 0.18s, border-color 0.18s;
  user-select: none; flex-shrink: 0;
}
.t-card.black { color: #0f172a; }
.t-card.red { color: #dc2626; }
.t-card.joker {
  color: #7c3aed; background: linear-gradient(135deg, #faf5ff, #f3e8ff); border-color: #c084fc;
}
.t-card-corner-top { display: flex; flex-direction: column; align-items: flex-start; line-height: 1; }
.t-card-corner-top .num { font-size: 15px; font-weight: 900; }
.t-card-corner-top .suit { font-size: 13px; }
.t-card-center-symbol {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
  font-size: 26px; pointer-events: none; opacity: 0.9;
}
.t-card-corner-bottom {
  display: flex; flex-direction: column; align-items: flex-end; line-height: 1; transform: rotate(180deg);
}
.t-card-corner-bottom .num { font-size: 15px; font-weight: 900; }
.t-card-corner-bottom .suit { font-size: 13px; }

/* Selection states */
.t-card.selected {
  transform: translateY(-16px); border: 2px solid #38bdf8;
  box-shadow: 0 12px 24px rgba(56, 189, 248, 0.4);
}
.t-card.poker-discard-selected {
  transform: translateY(-18px); border: 2px solid #f43f5e;
  box-shadow: 0 12px 24px rgba(244, 63, 94, 0.45);
}
.t-card.poker-discard-selected::after {
  content: "交換"; position: absolute; top: -10px; left: 50%; transform: translateX(-50%);
  background: #e11d48; color: #fff; font-size: 10px; font-weight: 800; padding: 1px 6px; border-radius: 9999px;
}
.t-card.unplayable { opacity: 0.42; filter: grayscale(0.5); cursor: not-allowed; }

/* Card Back (for opponents or deck) */
.t-card-back {
  width: var(--card-w); height: var(--card-h); border-radius: 8px;
  background: repeating-linear-gradient(45deg, #1e3a8a, #1e3a8a 6px, #172554 6px, #172554 12px);
  border: 2px solid #93c5fd; box-shadow: 0 4px 10px rgba(0,0,0,0.4); flex-shrink: 0;
}
.t-card-mini {
  width: 32px; height: 46px; border-radius: 4px; padding: 2px;
}
.t-card-mini .num { font-size: 10px; }
.t-card-mini .suit { font-size: 9px; }
.t-card-mini .t-card-center-symbol { font-size: 14px; }

/* Bottom Player Panel */
.player-bottom-panel {
  background: var(--panel-bg); border-top: 1px solid rgba(255,255,255,0.12);
  display: flex; flex-direction: column; padding: 8px 12px 14px; z-index: 40;
}
.action-bar {
  display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; flex-wrap: wrap; gap: 8px;
}
.my-info-pill {
  font-size: 13px; font-weight: 700; color: #cbd5e1; display: flex; align-items: center; gap: 8px;
}
.action-buttons { display: flex; align-items: center; gap: 8px; }
.btn-act {
  padding: 8px 16px; border-radius: 8px; font-weight: 800; font-size: 13px; cursor: pointer; border: none;
  transition: all 0.16s;
}
.btn-act.play { background: #0284c7; color: #fff; }
.btn-act.play:hover:not(:disabled) { background: #0369a1; transform: translateY(-1px); }
.btn-act.pass { background: rgba(255,255,255,0.12); color: #e2e8f0; }
.btn-act.pass:hover:not(:disabled) { background: rgba(255,255,255,0.2); }
.btn-act.danger { background: #e11d48; color: #fff; }
.btn-act.danger:hover:not(:disabled) { background: #be123c; }
.btn-act:disabled { opacity: 0.4; cursor: not-allowed; transform: none !important; }

.hand-cards-row {
  display: flex; gap: 8px; justify-content: center; align-items: flex-end; min-height: var(--card-h);
  overflow-x: auto; padding: 18px 4px 6px;
}

/* Modals */
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.7); backdrop-filter: blur(6px);
  display: flex; align-items: center; justify-content: center; z-index: 100; opacity: 0;
  pointer-events: none; transition: opacity 0.2s; padding: 16px;
}
.modal-overlay.show { opacity: 1; pointer-events: auto; }
.modal-box {
  background: #1e293b; border: 1px solid rgba(255,255,255,0.15); border-radius: 18px;
  width: 100%; max-width: 420px; padding: 24px; box-shadow: 0 20px 40px rgba(0,0,0,0.6);
  position: relative; animation: modalPop 0.24s var(--m3-easing);
}
@keyframes modalPop {
  0% { transform: scale(0.92) translateY(12px); opacity: 0; }
  100% { transform: scale(1) translateY(0); opacity: 1; }
}
.modal-close-btn {
  position: absolute; top: 16px; right: 16px; background: transparent; border: none;
  color: #94a3b8; font-size: 20px; cursor: pointer;
}
.modal-title { font-size: 18px; font-weight: 800; margin: 0 0 16px; color: #f8fafc; }

/* Auth Input Fields */
.form-group { margin-bottom: 14px; }
.form-label { display: block; font-size: 12px; font-weight: 700; color: #cbd5e1; margin-bottom: 6px; }
.input-with-toggle { position: relative; display: flex; align-items: center; }
.m3-input {
  width: 100%; background: #0f172a; border: 1px solid rgba(255,255,255,0.18); border-radius: 8px;
  padding: 10px 12px; color: #fff; font-size: 14px; outline: none; transition: border-color 0.2s;
}
.m3-input:focus { border-color: #38bdf8; }
.eye-toggle-btn {
  position: absolute; right: 10px; background: transparent; border: none; color: #94a3b8;
  cursor: pointer; display: flex; align-items: center; justify-content: center; padding: 4px;
}
.auth-error-msg { color: #f43f5e; font-size: 12px; font-weight: 700; margin-top: 8px; display: none; }

/* Standings Rows in Result Modal */
.standings-list { display: flex; flex-direction: column; gap: 8px; margin-bottom: 20px; }
.standing-item {
  display: flex; align-items: center; justify-content: space-between;
  background: rgba(255,255,255,0.06); padding: 10px 14px; border-radius: 10px; font-size: 14px;
}
.standing-item.me { background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.3); }
.standing-rank { font-weight: 800; width: 44px; color: #f59e0b; }
.standing-name { flex: 1; font-weight: 700; }
.standing-hand { font-weight: 800; color: #38bdf8; margin-right: 12px; }
.standing-rate-delta { font-weight: 800; font-size: 12px; }
.delta-plus { color: #4ade80; }
.delta-minus { color: #f43f5e; }

/* Toast cutin */
.toast-banner {
  position: absolute; top: 18%; left: 50%; transform: translateX(-50%);
  background: rgba(15, 23, 42, 0.94); border: 1px solid #f59e0b; color: #fde047;
  padding: 10px 24px; border-radius: 9999px; font-size: 15px; font-weight: 900;
  box-shadow: 0 10px 30px rgba(0,0,0,0.6); pointer-events: none; opacity: 0;
  transition: opacity 0.25s, transform 0.25s; z-index: 60; text-align: center;
}
.toast-banner.show { opacity: 1; transform: translate(-50%, -6px); }

.hidden { display: none !important; }
</style>
</head>
<body>
<div class="app-container">

  <!-- Top App Bar -->
  <header class="top-nav">
    <div class="brand">
      <a href="/">
        <span>🏠</span>
        <span>School Hub</span>
      </a>
      <span style="color:#64748b">/</span>
      <span style="color:#f8fafc;font-weight:700">トランプ</span>
    </div>
    <div class="nav-actions">
      <div class="user-chip" id="btnUserChip" onclick="openAuthModal()">
        <span id="navUserName">ゲスト</span>
        <span class="rate-badge">⭐️ <span id="navUserRate">1500</span> R</span>
      </div>
      <button type="button" class="btn-nav hidden" id="btnExitRoom" onclick="exitToLobby()">退室</button>
    </div>
  </header>

  <!-- 1. LOBBY VIEW -->
  <section class="lobby-screen" id="lobbyScreen">
    <div class="lobby-container">
      <div class="lobby-hero">
        <h1>🃏 オンライン対戦トランプ</h1>
        <p>4人対戦・最強AI・観戦対応・レートランキング機能付き</p>
      </div>

      <div class="auth-callout" id="authCallout">
        <span>💡 チャットと共通のアカウント（名前＆パスワード）でレートや戦績が記録されます</span>
        <button type="button" class="btn-primary-small" onclick="openAuthModal()">ログイン / 登録</button>
      </div>

      <div class="room-cards-grid" id="roomsGrid">
        <!-- Rendered dynamically -->
      </div>
    </div>
  </section>

  <!-- 2. GAME TABLE SCREEN -->
  <main class="game-screen hidden" id="gameScreen">
    <div class="table-board">

      <!-- Match In Progress Spectator Lock Banner -->
      <div class="match-lock-banner hidden" id="matchLockBanner">
        🔒 試合進行中（現在観戦中：この試合が終了次第、次の勝負に参加できます）
      </div>

      <!-- Opponent Seats -->
      <!-- North -->
      <div class="opp-seat opp-north" id="seat-rel-2">
        <div class="opp-avatar" id="av-rel-2">L</div>
        <div class="opp-info">
          <div class="opp-name-line">
            <span id="name-rel-2">レイ</span>
            <span class="opp-rate-tag" id="rate-rel-2">1550 R</span>
            <span class="opp-rank-tag" id="rank-rel-2">平民</span>
          </div>
          <div class="opp-cards-count" id="cnt-rel-2">残り 5枚</div>
          <div class="opp-showdown-cards" id="showdown-rel-2"></div>
        </div>
      </div>

      <!-- West -->
      <div class="opp-seat opp-west" id="seat-rel-1">
        <div class="opp-avatar" id="av-rel-1">K</div>
        <div class="opp-info">
          <div class="opp-name-line">
            <span id="name-rel-1">カイ</span>
            <span class="opp-rate-tag" id="rate-rel-1">1520 R</span>
            <span class="opp-rank-tag" id="rank-rel-1">平民</span>
          </div>
          <div class="opp-cards-count" id="cnt-rel-1">残り 5枚</div>
          <div class="opp-showdown-cards" id="showdown-rel-1"></div>
        </div>
      </div>

      <!-- East -->
      <div class="opp-seat opp-east" id="seat-rel-3">
        <div class="opp-avatar" id="av-rel-3">M</div>
        <div class="opp-info">
          <div class="opp-name-line">
            <span id="name-rel-3">ミナ</span>
            <span class="opp-rate-tag" id="rate-rel-3">1580 R</span>
            <span class="opp-rank-tag" id="rank-rel-3">平民</span>
          </div>
          <div class="opp-cards-count" id="cnt-rel-3">残り 5枚</div>
          <div class="opp-showdown-cards" id="showdown-rel-3"></div>
        </div>
      </div>

      <!-- Center Play Area -->
      <div class="center-field" id="centerField">
        <div class="poker-status-prompt hidden" id="pokerPrompt"></div>
        <div class="played-cards-row" id="centerPlayedCards"></div>
        <div class="play-caption hidden" id="centerPlayCaption"></div>
      </div>
    </div>

    <!-- Bottom Player Panel -->
    <div class="player-bottom-panel" id="bottomPanel">
      <div class="action-bar">
        <div class="my-info-pill" id="myStatusText">あなた (1500 R)</div>
        <div class="action-buttons">
          <!-- Daifugo Buttons -->
          <button type="button" class="btn-act play hidden" id="btnPlay" onclick="commitPlayerMove()" disabled>カードを出す</button>
          <button type="button" class="btn-act pass hidden" id="btnPass" onclick="playerPassTurn()" disabled>パス</button>

          <!-- Poker Buttons -->
          <button type="button" class="btn-act play hidden" id="btnPokerExchange" onclick="commitPokerExchange()">選択カードを交換 (0枚)</button>
          <button type="button" class="btn-act pass hidden" id="btnPokerStay" onclick="commitPokerStay()">チェンジなし (勝負)</button>

          <!-- Spectate toggle -->
          <button type="button" class="btn-act play hidden" id="btnTakeSeat" onclick="switchSpectate(false)">空き席に座る</button>
          <button type="button" class="btn-act pass hidden" id="btnGoSpectate" onclick="switchSpectate(true)">観戦に回る</button>
        </div>
      </div>
      <div class="hand-cards-row" id="myHandRow"></div>
    </div>
  </main>

  <!-- Round Result Standings Modal -->
  <div class="modal-overlay" id="resultModal">
    <div class="modal-box">
      <div class="modal-title">🏆 ラウンド結果・レート更新</div>
      <div class="standings-list" id="standingsList"></div>
      <button type="button" class="btn-act play" style="width:100%" onclick="sendRoomAction({action:'next_round'})">次の勝負へ進む</button>
    </div>
  </div>

  <!-- Shared OpenChat & Trump Auth Modal -->
  <div class="modal-overlay" id="authModal">
    <div class="modal-box">
      <button type="button" class="modal-close-btn" onclick="closeAuthModal()">✕</button>
      <div class="modal-title">👤 チャット＆トランプ ログイン</div>
      <div style="font-size:12px;color:#94a3b8;margin-bottom:14px">
        オープンチャットと同一のアカウントです。新規のお名前の場合は入力したパスワードで自動登録されます（名前被り防止・PBKDF2暗号化）。
      </div>
      <div class="form-group">
        <label class="form-label" for="authUsernameInput">プレイヤー名</label>
        <input type="text" id="authUsernameInput" class="m3-input" maxlength="20" placeholder="お名前を入力">
      </div>
      <div class="form-group">
        <label class="form-label" for="authPasswordInput">パスワード (4文字以上)</label>
        <div class="input-with-toggle">
          <input type="password" id="authPasswordInput" class="m3-input" maxlength="64" placeholder="パスワードを入力">
          <button type="button" class="eye-toggle-btn" id="btnPassToggle" onclick="togglePasswordVisibility()" aria-label="パスワード表示切替">
            <svg id="eyeIcon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
          </button>
        </div>
      </div>
      <div class="auth-error-msg" id="authErrorMsg"></div>
      <button type="button" class="btn-act play" style="width:100%;margin-top:10px" id="btnSubmitAuth" onclick="submitAuth()">ログイン / 新規登録</button>
    </div>
  </div>

  <!-- Toast Cutin -->
  <div class="toast-banner" id="toastBanner"></div>
</div>

<script>
// ============================================================================
// State & Client Initialization
// ============================================================================
let currentMode = null;
let currentRoom = null;
let currentPid = localStorage.getItem("schoolhub_trump_pid") || ("p_" + Math.random().toString(36).slice(2, 10));
localStorage.setItem("schoolhub_trump_pid", currentPid);

let authToken = localStorage.getItem("schoolhub_chat_token") || "";
let currentUsername = localStorage.getItem("schoolhub_chat_user") || "ゲスト";
let currentUserRate = 1500;

let socket = null;
let pollTimer = null;
let latestState = null;
let selectedHandIds = new Set();
let selectedDiscardIndices = new Set();
let sfxAudioContext = null;

// Sound Effects Synthesizer
function playSfx(type) {
  try {
    if (!sfxAudioContext) {
      sfxAudioContext = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (sfxAudioContext.state === "suspended") sfxAudioContext.resume();
    const ctx = sfxAudioContext;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    const now = ctx.currentTime;

    if (type === "card") {
      // Dealing card rustle sound
      osc.type = "sine";
      osc.frequency.setValueAtTime(480, now);
      osc.frequency.exponentialRampToValueAtTime(220, now + 0.08);
      gain.gain.setValueAtTime(0.18, now);
      gain.gain.exponentialRampToValueAtTime(0.01, now + 0.08);
      osc.start(now);
      osc.stop(now + 0.08);
    } else if (type === "select") {
      osc.type = "triangle";
      osc.frequency.setValueAtTime(540, now);
      gain.gain.setValueAtTime(0.12, now);
      gain.gain.exponentialRampToValueAtTime(0.01, now + 0.06);
      osc.start(now);
      osc.stop(now + 0.06);
    } else if (type === "special") {
      osc.type = "triangle";
      osc.frequency.setValueAtTime(523, now);
      osc.frequency.setValueAtTime(659, now + 0.1);
      osc.frequency.setValueAtTime(784, now + 0.2);
      gain.gain.setValueAtTime(0.25, now);
      gain.gain.exponentialRampToValueAtTime(0.01, now + 0.35);
      osc.start(now);
      osc.stop(now + 0.35);
    }
  } catch(e) {}
}

// ----------------------------------------------------------------------------
// Auth System (Tied to OpenChat Accounts & Rates)
// ----------------------------------------------------------------------------
async function checkAuthStatus() {
  if (!authToken) {
    updateAuthDisplay("ゲスト", 1500);
    return;
  }
  try {
    const res = await fetch(`/trump/api/auth/me?token=${encodeURIComponent(authToken)}`);
    if (res.ok) {
      const data = await res.json();
      currentUsername = data.username;
      currentUserRate = data.rate || 1500;
      updateAuthDisplay(currentUsername, currentUserRate);
    } else {
      authToken = "";
      localStorage.removeItem("schoolhub_chat_token");
      updateAuthDisplay("ゲスト", 1500);
    }
  } catch(e) {
    updateAuthDisplay(currentUsername, currentUserRate);
  }
}

function updateAuthDisplay(name, rate) {
  document.getElementById("navUserName").textContent = name;
  document.getElementById("navUserRate").textContent = rate;
  const callout = document.getElementById("authCallout");
  if (name !== "ゲスト") {
    callout.style.display = "none";
  } else {
    callout.style.display = "flex";
  }
}

function openAuthModal() {
  document.getElementById("authUsernameInput").value = (currentUsername !== "ゲスト") ? currentUsername : "";
  document.getElementById("authPasswordInput").value = "";
  document.getElementById("authErrorMsg").style.display = "none";
  document.getElementById("authModal").classList.add("show");
}

function closeAuthModal() {
  document.getElementById("authModal").classList.remove("show");
}

function togglePasswordVisibility() {
  const inp = document.getElementById("authPasswordInput");
  const isPass = (inp.type === "password");
  inp.type = isPass ? "text" : "password";
}

async function submitAuth() {
  const username = document.getElementById("authUsernameInput").value.trim();
  const password = document.getElementById("authPasswordInput").value;
  const errEl = document.getElementById("authErrorMsg");
  const btn = document.getElementById("btnSubmitAuth");
  errEl.style.display = "none";

  if (!username) {
    errEl.textContent = "お名前を入力してください";
    errEl.style.display = "block";
    return;
  }
  if (!password || password.length < 4) {
    errEl.textContent = "パスワードは4文字以上で設定してください";
    errEl.style.display = "block";
    return;
  }

  btn.disabled = true;
  btn.textContent = "認証中...";
  try {
    const res = await fetch("/trump/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, clientId: currentPid })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      authToken = data.token;
      currentUsername = data.username;
      currentUserRate = data.rate;
      localStorage.setItem("schoolhub_chat_token", authToken);
      localStorage.setItem("schoolhub_chat_user", currentUsername);
      updateAuthDisplay(currentUsername, currentUserRate);
      closeAuthModal();
      showToast(`ようこそ、${currentUsername}さん！ (⭐️ ${currentUserRate} R)`);
      if (currentMode) {
        connectToRoom(currentMode);
      } else {
        loadLobby();
      }
    } else {
      errEl.textContent = data.error || "ログインに失敗しました";
      errEl.style.display = "block";
    }
  } catch(e) {
    errEl.textContent = "通信エラーが発生しました";
    errEl.style.display = "block";
  } finally {
    btn.disabled = false;
    btn.textContent = "ログイン / 新規登録";
  }
}

// ----------------------------------------------------------------------------
// Lobby & Room Management
// ----------------------------------------------------------------------------
async function loadLobby() {
  try {
    const res = await fetch("/trump/api/lobby");
    const data = await res.json();
    renderLobby(data.rooms || []);
  } catch(e) {}
}

function renderLobby(rooms) {
  const grid = document.getElementById("roomsGrid");
  grid.innerHTML = "";
  for (const r of rooms) {
    const card = document.createElement("div");
    card.className = "room-card";
    const isPlaying = r.isMatchInProgress;
    const badgeCls = isPlaying ? "badge-playing" : "badge-open";
    const badgeTxt = isPlaying ? "🔒 試合中 (観戦可)" : "🟢 参加受付中";

    let playersHtml = "";
    if (r.seatedInfo && r.seatedInfo.length) {
      playersHtml = r.seatedInfo.map(p =>
        `<span class="player-tag">${p.name} <b style="color:#f59e0b">${p.rate}R</b></span>`
      ).join("");
    }

    card.innerHTML = `
      <div>
        <div class="room-card-title">
          <span>${r.title}</span>
          <span class="room-status-badge ${badgeCls}">${badgeTxt}</span>
        </div>
        <p class="room-card-desc">${r.description}</p>
        <div class="room-players-list">${playersHtml}</div>
        <div style="font-size:12px;color:#94a3b8">対戦中: ${r.humanCount}/4人 ・ 観戦者: ${r.spectatorCount}人</div>
      </div>
      <div class="room-actions">
        <button type="button" class="btn-join-room" onclick="enterRoom('${r.mode}', false)">${isPlaying ? '観戦して次の試合を待つ' : '入室して遊ぶ'}</button>
        <button type="button" class="btn-spec-room" onclick="enterRoom('${r.mode}', true)">観戦</button>
      </div>
    `;
    grid.appendChild(card);
  }
}

function enterRoom(mode, wantSpectate) {
  currentMode = mode;
  document.getElementById("lobbyScreen").classList.add("hidden");
  document.getElementById("gameScreen").classList.remove("hidden");
  document.getElementById("btnExitRoom").classList.remove("hidden");
  connectToRoom(mode, wantSpectate);
}

function exitToLobby() {
  if (socket) { socket.close(); socket = null; }
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  currentMode = null;
  document.getElementById("gameScreen").classList.add("hidden");
  document.getElementById("lobbyScreen").classList.remove("hidden");
  document.getElementById("btnExitRoom").classList.add("hidden");
  loadLobby();
}

// ----------------------------------------------------------------------------
// Room Connection (WebSocket with Polling Fallback)
// ----------------------------------------------------------------------------
function connectToRoom(mode, wantSpectate = false) {
  if (socket) { socket.close(); socket = null; }
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }

  const proto = (location.protocol === "https:") ? "wss:" : "ws:";
  const wsUrl = `${proto}//${location.host}/trump/ws/${mode}?pid=${encodeURIComponent(currentPid)}&name=${encodeURIComponent(currentUsername)}&token=${encodeURIComponent(authToken)}&spectate=${wantSpectate ? 1 : 0}`;

  try {
    socket = new WebSocket(wsUrl);
    socket.onmessage = (evt) => {
      try {
        const state = JSON.parse(evt.data);
        handleStateUpdate(state);
      } catch(e) {}
    };
    socket.onclose = () => {
      startPollingFallback(mode);
    };
    socket.onerror = () => {
      startPollingFallback(mode);
    };
  } catch(e) {
    startPollingFallback(mode);
  }
}

function startPollingFallback(mode) {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/trump/api/state/${mode}?pid=${encodeURIComponent(currentPid)}&name=${encodeURIComponent(currentUsername)}&token=${encodeURIComponent(authToken)}`);
      if (res.ok) {
        const state = await res.json();
        handleStateUpdate(state);
      }
    } catch(e) {}
  }, 1000);
}

function sendRoomAction(actionObj) {
  actionObj.pid = currentPid;
  actionObj.name = currentUsername;
  actionObj.token = authToken;

  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(actionObj));
  } else if (currentMode) {
    fetch(`/trump/api/action/${currentMode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(actionObj)
    }).then(res => res.json()).then(state => handleStateUpdate(state)).catch(()=>{});
  }
}

// ----------------------------------------------------------------------------
// State Handler & UI Rendering
// ----------------------------------------------------------------------------
function handleStateUpdate(state) {
  latestState = state;

  // Match in progress banner
  const lockBanner = document.getElementById("matchLockBanner");
  if (state.isSpectator && state.isMatchInProgress) {
    lockBanner.classList.remove("hidden");
  } else {
    lockBanner.classList.add("hidden");
  }

  // Render Opponent Seats (relPos 1=West, 2=North, 3=East)
  for (const s of state.seats) {
    if (s.relPos === 0) continue;
    const rel = s.relPos;
    const seatEl = document.getElementById(`seat-rel-${rel}`);
    if (!seatEl) continue;

    seatEl.classList.toggle("active-turn", s.isTurn);
    document.getElementById(`name-rel-${rel}`).textContent = s.name;
    document.getElementById(`rate-rel-${rel}`).textContent = `${s.rate || 1500} R`;
    document.getElementById(`rank-rel-${rel}`).textContent = s.rankTitle;

    const cntEl = document.getElementById(`cnt-rel-${rel}`);
    cntEl.textContent = `残り ${s.cardCount}枚`;

    // Poker Showdown reveal cards
    const showdownBox = document.getElementById(`showdown-rel-${rel}`);
    showdownBox.innerHTML = "";
    if (s.showdownCards && s.showdownCards.length) {
      for (const c of s.showdownCards) {
        showdownBox.appendChild(createCardElement(c, true));
      }
    }
  }

  // Center Field Area
  renderCenterField(state);

  // Bottom Player Panel & Hands (with deal motion)
  renderPlayerBottomPanel(state);

  // Result Modal
  const resModal = document.getElementById("resultModal");
  if (state.phase === "round_end") {
    renderStandingsModal(state);
    resModal.classList.add("show");
  } else {
    resModal.classList.remove("show");
  }

  // Audio / Events Cutin
  if (state.events && state.events.length) {
    const last = state.events[state.events.length - 1];
    if (last && last.text && (!window._lastEventSeq || last.seq > window._lastEventSeq)) {
      window._lastEventSeq = last.seq;
      if (last.kind === "cutin") showToast(last.text);
      if (last.sfx) playSfx(last.sfx);
    }
  }
}

function renderCenterField(state) {
  const centerPlayed = document.getElementById("centerPlayedCards");
  const centerCap = document.getElementById("centerPlayCaption");
  const promptEl = document.getElementById("pokerPrompt");

  centerPlayed.innerHTML = "";
  centerCap.classList.add("hidden");
  promptEl.classList.add("hidden");

  if (state.mode === "poker") {
    if (state.phase === "playing") {
      promptEl.classList.remove("hidden");
      const isMyTurn = (!state.isSpectator && state.currentTurn === state.mySeat);
      if (isMyTurn) {
        promptEl.textContent = "⚡️ あなたの番です: 交換するカードを選んで「カードを交換」を押してください";
      } else {
        const turnName = (state.seats.find(s => s.seat === state.currentTurn) || {}).name || "相手";
        promptEl.textContent = `⏳ ${turnName} が手札を吟味中...`;
      }
    } else if (state.tablePlay) {
      // Showdown winning hand highlight
      for (const c of state.tablePlay.cards || []) {
        centerPlayed.appendChild(createCardElement(c, false));
      }
      centerCap.textContent = state.tablePlay.label || "";
      centerCap.classList.remove("hidden");
    }
  } else {
    // Daifugo table trick
    if (state.tablePlay && state.tablePlay.cards) {
      for (const c of state.tablePlay.cards) {
        centerPlayed.appendChild(createCardElement(c, false));
      }
      if (state.tablePlay.label) {
        centerCap.textContent = state.tablePlay.label;
        centerCap.classList.remove("hidden");
      }
    }
  }
}

function renderPlayerBottomPanel(state) {
  const isSpec = state.isSpectator;
  const myInfo = state.seats.find(s => s.relPos === 0);
  const statusTxt = document.getElementById("myStatusText");
  const myRate = myInfo ? (myInfo.rate || 1500) : currentUserRate;

  const btnPlay = document.getElementById("btnPlay");
  const btnPass = document.getElementById("btnPass");
  const btnExchange = document.getElementById("btnPokerExchange");
  const btnStay = document.getElementById("btnPokerStay");
  const btnTakeSeat = document.getElementById("btnTakeSeat");
  const btnGoSpec = document.getElementById("btnGoSpectate");
  const handRow = document.getElementById("myHandRow");

  if (isSpec) {
    statusTxt.textContent = `観戦中 (${currentUsername} ・ ${myRate} R)`;
    btnPlay.classList.add("hidden");
    btnPass.classList.add("hidden");
    btnExchange.classList.add("hidden");
    btnStay.classList.add("hidden");
    btnGoSpec.classList.add("hidden");

    if (state.isMatchInProgress) {
      btnTakeSeat.classList.remove("hidden");
      btnTakeSeat.disabled = true;
      btnTakeSeat.textContent = "試合終了まで着席不可";
    } else {
      btnTakeSeat.classList.remove("hidden");
      btnTakeSeat.disabled = false;
      btnTakeSeat.textContent = "空き席に座る";
    }

    handRow.innerHTML = `<div style="color:#94a3b8;font-size:13px;padding:20px">
      ${state.isMatchInProgress ? "🔒 現在試合中のため観戦モードです。この試合が終了次第、空き席に着席できます。" : "🟢 現在観戦モードです。「空き席に座る」を押して対戦に参加できます。"}
    </div>`;
    return;
  }

  btnTakeSeat.classList.add("hidden");
  btnGoSpec.classList.remove("hidden");
  statusTxt.textContent = `${currentUsername} (⭐️ ${myRate} R) ・ 手札 ${state.myHand.length}枚`;

  const isMyTurn = (state.phase === "playing" && state.currentTurn === state.mySeat);

  if (state.mode === "poker") {
    btnPlay.classList.add("hidden");
    btnPass.classList.add("hidden");
    btnExchange.classList.remove("hidden");
    btnStay.classList.remove("hidden");

    const exchangeCount = selectedDiscardIndices.size;
    btnExchange.textContent = `選択カードを交換 (${exchangeCount}枚)`;
    btnExchange.disabled = !isMyTurn;
    btnStay.disabled = !isMyTurn;
  } else {
    // Daifugo Buttons
    btnExchange.classList.add("hidden");
    btnStay.classList.add("hidden");
    btnPlay.classList.remove("hidden");
    btnPass.classList.remove("hidden");

    btnPlay.disabled = (!isMyTurn || selectedHandIds.size === 0);
    btnPass.disabled = (!isMyTurn || !state.tablePlay);
  }

  // Render Hand Cards with Physical Deal Motion (配るモーション)
  handRow.innerHTML = "";
  state.myHand.forEach((card, idx) => {
    let isSelected = false;
    let extraCls = "";

    if (state.mode === "poker") {
      isSelected = selectedDiscardIndices.has(idx);
      if (isSelected) extraCls = "poker-discard-selected";
    } else {
      isSelected = selectedHandIds.has(card.id);
      if (isSelected) extraCls = "selected";
      const playable = new Set(state.playableIds || []);
      if (isMyTurn && state.tablePlay && !playable.has(card.id)) {
        extraCls += " unplayable";
      }
    }

    const cardEl = createCardElement(card, false, extraCls);
    // Add physical deal animation with staggered delay
    cardEl.classList.add("card-dealing");
    cardEl.style.animationDelay = `${idx * 0.08}s`;

    cardEl.addEventListener("click", () => {
      if (!isMyTurn) return;
      playSfx("select");
      if (state.mode === "poker") {
        if (selectedDiscardIndices.has(idx)) selectedDiscardIndices.delete(idx);
        else selectedDiscardIndices.add(idx);
      } else {
        if (selectedHandIds.has(card.id)) selectedHandIds.delete(card.id);
        else selectedHandIds.add(card.id);
      }
      renderPlayerBottomPanel(state);
    });

    handRow.appendChild(cardEl);
  });
}

function createCardElement(card, isMini = false, extraCls = "") {
  const el = document.createElement("div");
  const colorCls = card.isJoker ? "joker" : (card.color === "red" ? "red" : "black");
  el.className = `t-card ${colorCls} ${isMini ? 't-card-mini' : ''} ${extraCls}`;

  if (card.isJoker) {
    el.innerHTML = `
      <div class="t-card-corner-top"><span class="num">J</span><span class="suit">★</span></div>
      <div class="t-card-center-symbol">🃏</div>
      <div class="t-card-corner-bottom"><span class="num">J</span><span class="suit">★</span></div>
    `;
  } else {
    el.innerHTML = `
      <div class="t-card-corner-top"><span class="num">${card.label}</span><span class="suit">${card.suitSymbol}</span></div>
      <div class="t-card-center-symbol">${card.suitSymbol}</div>
      <div class="t-card-corner-bottom"><span class="num">${card.label}</span><span class="suit">${card.suitSymbol}</span></div>
    `;
  }
  return el;
}

function renderStandingsModal(state) {
  const list = document.getElementById("standingsList");
  list.innerHTML = "";
  for (const st of state.standings || []) {
    const item = document.createElement("div");
    item.className = `standing-item ${st.isMe ? 'me' : ''}`;
    const delta = st.rateDelta || 0;
    const deltaCls = delta >= 0 ? "delta-plus" : "delta-minus";
    const deltaTxt = delta >= 0 ? `+${delta}` : `${delta}`;

    item.innerHTML = `
      <div class="standing-rank">${st.place}位</div>
      <div class="standing-name">${st.name} ${st.isMe ? '(あなた)' : ''}</div>
      <div class="standing-hand">${st.rankTitle}</div>
      <div class="standing-rate-delta ${deltaCls}">${deltaTxt} R (新: ${st.newRate || st.rate} R)</div>
    `;
    list.appendChild(item);
  }
}

// ----------------------------------------------------------------------------
// Action Triggers
// ----------------------------------------------------------------------------
function commitPlayerMove() {
  if (selectedHandIds.size === 0) return;
  sendRoomAction({ action: "play", cardIds: Array.from(selectedHandIds) });
  selectedHandIds.clear();
}

function playerPassTurn() {
  sendRoomAction({ action: "pass" });
  selectedHandIds.clear();
}

function commitPokerExchange() {
  const indices = Array.from(selectedDiscardIndices);
  sendRoomAction({ action: "poker_exchange", cardIndices: indices });
  selectedDiscardIndices.clear();
}

function commitPokerStay() {
  sendRoomAction({ action: "poker_stay" });
  selectedDiscardIndices.clear();
}

function switchSpectate(wantSpectate) {
  sendRoomAction({ action: "toggle_spectate", spectate: wantSpectate });
}

function showToast(msg) {
  const banner = document.getElementById("toastBanner");
  banner.textContent = msg;
  banner.classList.add("show");
  clearTimeout(window._toastTimeout);
  window._toastTimeout = setTimeout(() => banner.classList.remove("show"), 2800);
}

// Initial Boot
window.addEventListener("DOMContentLoaded", () => {
  checkAuthStatus();
  loadLobby();
  setInterval(loadLobby, 4000);
});
</script>
</body>
</html>
"""


@router.get("/trump", response_class=HTMLResponse)
@router.get("/trump/", response_class=HTMLResponse)
async def trump_game_page():
    return HTMLResponse(TRUMP_HTML)
