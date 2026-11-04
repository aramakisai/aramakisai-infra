"""当日監視の画面表記。キーと文言の正本は design.md「画面表記」。"""

LABELS: dict[str, str] = {
    "fest.title": "当日監視",
    "fest.status.ok": "正常",
    "fest.status.warn": "注意",
    "fest.status.crit": "異常",
    "fest.status.stale": "取得遅延",
    "fest.status.error": "取得失敗",
    "fest.status.empty": "時間外",
    "fest.status.info": "表示のみ",
    "fest.tab.visitor": "来場者影響",
    "fest.tab.function": "機能別",
    "fest.tab.capacity": "容量",
    "fest.tab.infra": "基盤・セキュリティ",
    "fest.band.counts": "異常 {crit} 件 / 注意 {warn} 件",
    "fest.band.updated": "最終更新 {time}",
    "fest.band.stale": "表示が古い (最終更新 {time})",
    "fest.band.reauth": "表示が古い・再ログインが必要",
    "fest.band.eval_stopped": "判定が止まっている (最終判定 {time})",
    "fest.band.more": "ほか {n} 件",
    "fest.detail.threshold": "閾値",
    "fest.detail.fetched_at": "取得時刻",
    "fest.detail.observed_at": "データの時刻",
    "fest.detail.link": "詳細を見る",
    "fest.detail.gone": "この項目は現在ありません",
    "fest.unconfigured": "当日監視の宣言がありません",
}


def label(key: str, **kw) -> str:
    """未定義キーは KeyError (typo を黙って通さない)。"""
    s = LABELS[key]
    return s.format(**kw) if kw else s
