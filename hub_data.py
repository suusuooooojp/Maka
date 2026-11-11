"""School Hub UI data helpers."""

_SERVICE_META = [
    (
        "minecraft",
        {
            "category": "Games",
            "icon": "MC",
            "icon_key": "box",
            "description": "マインクラフト 1.5.2 (日本語版・独自鯖・分散処理)",
            "keywords": "minecraft eaglercraft マイクラ マインクラフト 日本語 独自サーバー ゲーム games play",
            "accent": "#15803d",
        },
    ),
    (
        "eagler",
        {
            "category": "Games",
            "icon": "MC",
            "icon_key": "box",
            "description": "マインクラフト 1.5.2 (日本語版・独自鯖・分散処理)",
            "keywords": "minecraft eaglercraft マイクラ マインクラフト 日本語 独自サーバー ゲーム games play",
            "accent": "#15803d",
        },
    ),
    (
        "bloxd",
        {
            "category": "Games",
            "icon": "BX",
            "icon_key": "box",
            "description": "3Dボクセル マルチプレイ",
            "keywords": "bloxd ブロックスド マイクラ マインクラフト ゲーム games play",
            "accent": "#d97706",
        },
    ),
    (
        "florr",
        {
            "category": "Games",
            "icon": "FL",
            "icon_key": "compass",
            "description": "2Dマルチプレイ アクション",
            "keywords": "florr フロア 花びら io ゲーム マルチ games",
            "accent": "#16a34a",
        },
    ),
    (
        "taiko",
        {
            "category": "Games",
            "icon": "TK",
            "icon_key": "disc",
            "description": "太鼓の達人 リズムゲーム",
            "keywords": "taiko 太鼓 太鼓の達人 リズムゲーム 音ゲー open taiko games",
            "accent": "#ea580c",
        },
    ),
    (
        "cloudmoon",
        {
            "category": "Games",
            "icon": "CM",
            "icon_key": "cloud",
            "description": "無料クラウドゲーム・300タイトル以上",
            "keywords": "cloudmoon クラウドムーン cloud moon クラウドゲーム スマホゲーム games play",
            "accent": "#6366f1",
        },
    ),
    (
        "trump",
        {
            "category": "Games",
            "icon": "TR",
            "icon_key": "layers",
            "description": "1人称4人テーブル・大富豪＆ポーカー(最強AI・レート対応)",
            "keywords": "trump トランプ 大富豪 大貧民 ポーカー テキサス ドロー レート カードゲーム テーブルゲーム 4人 1人称 games",
            "accent": "#e11d48",
        },
    ),
    (
        "diep",
        {
            "category": "Games",
            "icon": "DI",
            "icon_key": "gamepad",
            "description": "戦車シューティング 2Dマルチプレイ",
            "keywords": "diep.io deep.io ディープ ダイep 戦車 タンク io ゲーム マルチ games",
            "accent": "#0ea5e9",
        },
    ),
    (
        "deep",
        {
            "category": "Games",
            "icon": "DI",
            "icon_key": "gamepad",
            "description": "戦車シューティング 2Dマルチプレイ",
            "keywords": "diep.io deep.io ディープ ダイep 戦車 タンク io ゲーム マルチ games",
            "accent": "#0ea5e9",
        },
    ),
    (
        "youtube(beta)",
        {
            "category": "Watch",
            "icon": "YB",
            "icon_key": "play-circle",
            "description": "本物YouTube & 全サイト対応・完全ゼロサーバー直接通信 (Render 0%)",
            "keywords": "youtube beta ユーチューブ ベータ 直接通信 render非経由 全サイト ブラウザ プロキシ 動画 ショート 音楽 配信 watch",
            "accent": "#ff0033",
        },
    ),
    (
        "youtube",
        {
            "category": "Watch",
            "icon": "YT",
            "icon_key": "tv",
            "description": "動画・ショート・検索・ゲーム (Playables)",
            "keywords": "youtube ユーチューブ 動画 ショート 音楽 配信 watch ゲーム プレイアブル playables games",
            "accent": "#dc2626",
        },
    ),
    (
        "わかめ",
        {
            "category": "Watch",
            "icon": "WK",
            "icon_key": "film",
            "description": "軽量YouTubeクライアント",
            "keywords": "wakame わかめ ぷらす youtube 動画 音楽 軽量 watch",
            "accent": "#059669",
        },
    ),
    (
        "twitch",
        {
            "category": "Watch",
            "icon": "TW",
            "icon_key": "radio",
            "description": "ライブ配信視聴",
            "keywords": "twitch ツイッチ 配信 ライブ ゲーム実況 watch",
            "accent": "#7c3aed",
        },
    ),
    (
        "spotify",
        {
            "category": "Watch",
            "icon": "SP",
            "icon_key": "music",
            "description": "音楽ストリーミング・ログイン再生",
            "keywords": "spotify スポティファイ 音楽 ミュージック 曲 再生 プレイリスト watch music song audio",
            "accent": "#1db954",
        },
    ),
    (
        "instagram",
        {
            "category": "Watch",
            "icon": "IG",
            "icon_key": "camera",
            "description": "リール・投稿閲覧",
            "keywords": "instagram インスタ インスタグラム リール 写真 sns watch",
            "accent": "#db2777",
        },
    ),
    (
        "tiktok",
        {
            "category": "Watch",
            "icon": "TT",
            "icon_key": "video",
            "description": "ショート動画・ログイン・軽量視聴",
            "keywords": "tiktok ティックトック ショート動画 ダンス 音楽 トレンド ログイン sns watch video",
            "accent": "#ff0050",
        },
    ),
    (
        "interstellar",
        {
            "category": "Browser",
            "icon": "IS",
            "icon_key": "globe",
            "description": "Webプロキシ・URL直接移動",
            "keywords": "interstellar インターステラー ブラウザ 検索 web proxy browser",
            "accent": "#2563eb",
        },
    ),
    (
        "utopia",
        {
            "category": "Browser",
            "icon": "UT",
            "icon_key": "compass",
            "description": "Webプロキシ・内蔵ゲーム",
            "keywords": "utopia ユートピア プロキシ ブラウザ ゲーム web proxy games browser",
            "accent": "#0891b2",
        },
    ),
    (
        "kazwire",
        {
            "category": "Browser",
            "icon": "KZ",
            "icon_key": "shield",
            "description": "Webプロキシ・内蔵ゲーム",
            "keywords": "kazwire カズワイヤー プロキシ ブラウザ ゲーム web proxy games browser",
            "accent": "#f59e0b",
        },
    ),
    (
        "tver",
        {
            "category": "Watch",
            "icon": "TV",
            "icon_key": "tv",
            "description": "民放公式テレビ配信 (見逃し配信・リアルタイム配信)",
            "keywords": "tver ティーバー テレビ ドラマ バラエティ アニメ ニュース 見逃し配信 リアルタイム配信 民放 公式 動画 watch tv",
            "accent": "#00b2ff",
        },
    ),
    (
        "line",
        {
            "category": "Watch",
            "icon": "LN",
            "icon_key": "message-circle",
            "description": "LINE メッセンジャー (QRログイン・トーク・E2EE対応・OkLine搭載)",
            "keywords": "line ライン トーク チャット メッセージ qrログイン スタンプ 通話 友だち グループ okline sns watch",
            "accent": "#06c755",
        },
    ),
    (
        "x",
        {
            "category": "Watch",
            "icon": "X",
            "icon_key": "share-2",
            "description": "タイムライン・検索",
            "keywords": "x twitter ツイッター sns トレンド ポスト watch",
            "accent": "#334155",
        },
    ),
]

CATEGORY_LABELS = {
    "Games": "ゲーム",
    "Watch": "動画・SNS",
    "Browser": "ブラウザ",
    "Tools": "ツール",
}


def _match_meta(name: str, path: str = "") -> dict:
    nl = (name or "").strip().lower()
    pl = (path or "").strip().lower()
    for key, meta in _SERVICE_META:
        if key == "x":
            if nl == "x" or pl.startswith("/x"):
                return meta
        elif key == "line":
            if nl == "line" or pl.startswith("/line"):
                return meta
        elif key == "tver":
            if nl == "tver" or pl.startswith("/tver"):
                return meta
        elif key in nl or key in pl:
            return meta
    code = (name or "SH").strip()[:2].upper()
    return {
        "category": "Tools",
        "icon": code,
        "icon_key": "grid",
        "description": path or "/",
        "keywords": f"{name} {path} tools",
        "accent": "#2563eb",
    }


def enrich_service(item):
    name = item.get("name", "Service")
    path = item.get("path", "/")
    meta = _match_meta(name, path)
    category = meta["category"]
    return {
        **item,
        "category": category,
        "category_label": CATEGORY_LABELS.get(category, "その他"),
        "icon": meta["icon"],
        "icon_key": meta.get("icon_key", "grid"),
        "description": meta["description"],
        "keywords": meta["keywords"],
        "accent": meta["accent"],
    }
