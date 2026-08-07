"""Generate slides.json with sample Japanese proposal slides."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

INDUSTRIES = ["小売", "製造", "広告", "EC", "金融", "通信", "ヘルスケア", "人材"]
PROPOSAL_TYPES = ["新規提案", "現状分析", "施策提案", "効果検証", "ロードマップ", "競合比較"]
GRAPH_TYPES = ["棒グラフ", "折れ線", "円グラフ", "ファネル", "散布図", "テーブル", "ロードマップ", "なし"]
LAYOUT_TYPES = ["タイトル中央", "左右比較", "上下分割", "4象限", "Before/After", "ロードマップ", "リスト"]

SLIDES = [
    {
        "industry": "小売", "proposalType": "現状分析", "graphType": "棒グラフ", "layoutType": "タイトル中央",
        "title": "店舗別売上の四半期推移", "tags": ["売上", "店舗別", "四半期"],
        "summary": "首都圏10店舗の四半期売上を比較し、上位3店舗と下位3店舗の差分要因を整理。",
        "reuseHint": "小売クライアントの初回ヒアリングで現状把握ページとして再利用可能。",
    },
    {
        "industry": "小売", "proposalType": "施策提案", "graphType": "ファネル", "layoutType": "上下分割",
        "title": "来店から購買までの離脱率改善案", "tags": ["CVR", "ファネル", "店頭"],
        "summary": "来店→試着→レジまでの離脱を可視化し、試着導線の改善で+8ptを試算。",
        "reuseHint": "アパレル系の施策提案セクション冒頭で利用想定。",
    },
    {
        "industry": "小売", "proposalType": "競合比較", "graphType": "4象限", "layoutType": "4象限",
        "title": "競合ポジショニングマップ", "tags": ["競合", "ポジショニング"],
        "summary": "価格帯×ブランド認知の2軸で主要競合8社をマッピング。",
        "reuseHint": "新規ブランド立ち上げ提案の市場理解パートで再利用。",
    },
    {
        "industry": "製造", "proposalType": "現状分析", "graphType": "折れ線", "layoutType": "タイトル中央",
        "title": "工場稼働率の月次推移", "tags": ["稼働率", "製造KPI"],
        "summary": "過去24ヶ月の工場稼働率を月次で示し、季節要因と保守停止の影響を分離。",
        "reuseHint": "製造業オペレーション改善案件の現状整理で再利用。",
    },
    {
        "industry": "製造", "proposalType": "施策提案", "graphType": "ロードマップ", "layoutType": "ロードマップ",
        "title": "スマートファクトリー化ロードマップ", "tags": ["DX", "ロードマップ", "IoT"],
        "summary": "センサ導入→データ統合→AI最適化の3フェーズを4四半期で計画。",
        "reuseHint": "製造業DX提案の中盤、施策全体像として再利用可能。",
    },
    {
        "industry": "製造", "proposalType": "効果検証", "graphType": "棒グラフ", "layoutType": "Before/After",
        "title": "予知保全導入前後の停止時間比較", "tags": ["保全", "ROI"],
        "summary": "ライン単位での計画外停止時間を導入前後で比較し、平均42%削減を確認。",
        "reuseHint": "効果検証パートのキースライド。数値だけ差し替えて流用可。",
    },
    {
        "industry": "広告", "proposalType": "新規提案", "graphType": "円グラフ", "layoutType": "左右比較",
        "title": "媒体別予算配分の見直し案", "tags": ["メディアプラン", "予算配分"],
        "summary": "現行配分と提案配分を並列で示し、運用型広告比率を引き上げる根拠を整理。",
        "reuseHint": "広告主への年次プラン提案で再利用。クライアント名のみ差し替え。",
    },
    {
        "industry": "広告", "proposalType": "効果検証", "graphType": "折れ線", "layoutType": "タイトル中央",
        "title": "キャンペーン期間中のCTR推移", "tags": ["CTR", "クリエイティブ"],
        "summary": "クリエイティブ差替えタイミングと連動したCTRの変化を可視化。",
        "reuseHint": "実績報告会のサマリーページとして再利用。",
    },
    {
        "industry": "広告", "proposalType": "施策提案", "graphType": "テーブル", "layoutType": "リスト",
        "title": "クリエイティブABテスト計画", "tags": ["ABテスト", "クリエイティブ"],
        "summary": "訴求軸×フォーマットの組み合わせ12案を優先度付きで一覧化。",
        "reuseHint": "クリエイティブ提案の計画パートで利用。",
    },
    {
        "industry": "EC", "proposalType": "現状分析", "graphType": "ファネル", "layoutType": "上下分割",
        "title": "EC購買ファネルの離脱分析", "tags": ["CVR", "ファネル", "EC"],
        "summary": "TOP→商品詳細→カート→購入の各段階離脱率を直近3ヶ月で算出。",
        "reuseHint": "EC案件の初回提案で必須の現状把握スライド。",
    },
    {
        "industry": "EC", "proposalType": "施策提案", "graphType": "なし", "layoutType": "4象限",
        "title": "顧客セグメント別アプローチ案", "tags": ["セグメント", "CRM"],
        "summary": "RFM分析で得た4象限ごとに、施策方針とKPIを定義。",
        "reuseHint": "CRM提案のセグメント設計パートで再利用。",
    },
    {
        "industry": "EC", "proposalType": "効果検証", "graphType": "棒グラフ", "layoutType": "Before/After",
        "title": "レコメンド導入後のクロスセル効果", "tags": ["レコメンド", "クロスセル", "AOV"],
        "summary": "レコメンド導入の前後で平均購買単価が17%改善した実績を提示。",
        "reuseHint": "EC効果報告会のハイライトスライド。",
    },
    {
        "industry": "金融", "proposalType": "新規提案", "graphType": "ロードマップ", "layoutType": "ロードマップ",
        "title": "デジタルチャネル統合のロードマップ", "tags": ["DX", "チャネル統合"],
        "summary": "アプリ・Web・店舗のシームレス化を3年で完成させる工程表。",
        "reuseHint": "金融DX提案の全体構想ページとして再利用。",
    },
    {
        "industry": "金融", "proposalType": "現状分析", "graphType": "テーブル", "layoutType": "リスト",
        "title": "規制要件と対応状況のマッピング", "tags": ["コンプライアンス", "規制"],
        "summary": "主要規制10項目に対する自社対応状況をRAGステータスで一覧化。",
        "reuseHint": "金融機関向けコンプライアンス提案の前提整理で利用。",
    },
    {
        "industry": "金融", "proposalType": "競合比較", "graphType": "散布図", "layoutType": "4象限",
        "title": "ネット銀行各社の機能比較", "tags": ["競合", "機能比較"],
        "summary": "UX評価×手数料水準の散布図で主要ネット銀行10社を比較。",
        "reuseHint": "金融プロダクト改善提案の競合分析で再利用。",
    },
    {
        "industry": "通信", "proposalType": "施策提案", "graphType": "ファネル", "layoutType": "上下分割",
        "title": "MNP獲得チャネルの最適化", "tags": ["MNP", "獲得", "チャネル"],
        "summary": "オンライン・量販・直営の3チャネルでCPA差を可視化し、配分を再設計。",
        "reuseHint": "通信キャリア向け獲得戦略提案で再利用。",
    },
    {
        "industry": "通信", "proposalType": "効果検証", "graphType": "折れ線", "layoutType": "タイトル中央",
        "title": "解約率改善施策の四半期効果", "tags": ["解約率", "リテンション"],
        "summary": "プラン見直しとロイヤリティ施策の重ね打ちで解約率0.8pt改善。",
        "reuseHint": "リテンション系提案の効果ページで再利用。",
    },
    {
        "industry": "通信", "proposalType": "現状分析", "graphType": "棒グラフ", "layoutType": "タイトル中央",
        "title": "エリア別ARPU比較", "tags": ["ARPU", "エリア"],
        "summary": "全国8エリアのARPUを横並び比較し、首都圏との乖離要因を抽出。",
        "reuseHint": "通信事業者向け収益改善案件の事前分析で利用。",
    },
    {
        "industry": "ヘルスケア", "proposalType": "新規提案", "graphType": "なし", "layoutType": "左右比較",
        "title": "患者向けアプリ開発の全体像", "tags": ["アプリ", "PHR"],
        "summary": "現状の紙ベース運用と新アプリ運用のフロー比較。",
        "reuseHint": "病院・クリニック向けデジタル化提案の中心スライド。",
    },
    {
        "industry": "ヘルスケア", "proposalType": "施策提案", "graphType": "ロードマップ", "layoutType": "ロードマップ",
        "title": "電子カルテ移行ロードマップ", "tags": ["電子カルテ", "移行"],
        "summary": "3病院の電子カルテ移行を半年で順次実施する工程。",
        "reuseHint": "医療機関のシステム刷新提案で再利用。",
    },
    {
        "industry": "ヘルスケア", "proposalType": "現状分析", "graphType": "円グラフ", "layoutType": "タイトル中央",
        "title": "外来患者の受診経路内訳", "tags": ["外来", "患者導線"],
        "summary": "紹介・直接・Web予約の比率を可視化し、Web予約強化の余地を明示。",
        "reuseHint": "医療機関の集患提案で再利用。",
    },
    {
        "industry": "人材", "proposalType": "施策提案", "graphType": "ファネル", "layoutType": "上下分割",
        "title": "採用ファネル改善プラン", "tags": ["採用", "ファネル"],
        "summary": "応募→面接→内定→入社の各段階での歩留まりと改善ポイントを整理。",
        "reuseHint": "人事コンサル案件の施策パートで再利用。",
    },
    {
        "industry": "人材", "proposalType": "現状分析", "graphType": "テーブル", "layoutType": "リスト",
        "title": "職種別離職率の一覧", "tags": ["離職", "人事KPI"],
        "summary": "営業・開発・コーポレートの主要15職種で離職率を比較。",
        "reuseHint": "人事戦略提案のファクトページで再利用。",
    },
    {
        "industry": "人材", "proposalType": "効果検証", "graphType": "棒グラフ", "layoutType": "Before/After",
        "title": "1on1導入後のエンゲージメント変化", "tags": ["1on1", "エンゲージメント"],
        "summary": "導入前後の調査スコアを部門別に並べ、平均+11ptを確認。",
        "reuseHint": "組織開発提案の成果報告で再利用。",
    },
    {
        "industry": "小売", "proposalType": "ロードマップ", "graphType": "ロードマップ", "layoutType": "ロードマップ",
        "title": "OMO推進ロードマップ", "tags": ["OMO", "ロードマップ"],
        "summary": "アプリ刷新→在庫統合→店頭体験再設計の3段階を1年で実行。",
        "reuseHint": "小売OMO提案の全体構想ページ。",
    },
    {
        "industry": "EC", "proposalType": "競合比較", "graphType": "散布図", "layoutType": "4象限",
        "title": "EC競合のUX vs 価格マップ", "tags": ["競合", "UX"],
        "summary": "主要EC15社をUX評価×平均価格で散布し、空白ポジションを特定。",
        "reuseHint": "新規EC立ち上げ提案の市場理解パート。",
    },
    {
        "industry": "金融", "proposalType": "施策提案", "graphType": "なし", "layoutType": "左右比較",
        "title": "店舗業務のデジタル化Before/After", "tags": ["店舗", "業務改善"],
        "summary": "窓口業務の現行フローと提案フローを並列で示す。",
        "reuseHint": "金融機関の店舗改革提案で再利用。",
    },
    {
        "industry": "広告", "proposalType": "ロードマップ", "graphType": "ロードマップ", "layoutType": "ロードマップ",
        "title": "年間メディアプラン", "tags": ["メディアプラン", "年間"],
        "summary": "繁忙期・閑散期に応じた媒体配分の年間ロードマップ。",
        "reuseHint": "広告主への年次プラン提案で再利用。",
    },
    {
        "industry": "通信", "proposalType": "新規提案", "graphType": "4象限", "layoutType": "4象限",
        "title": "新サービスのターゲット定義", "tags": ["セグメント", "新規"],
        "summary": "利用頻度×単価の2軸で4象限を定義し、コアターゲットを設定。",
        "reuseHint": "通信新サービス立ち上げ提案で再利用。",
    },
    {
        "industry": "ヘルスケア", "proposalType": "効果検証", "graphType": "折れ線", "layoutType": "タイトル中央",
        "title": "予約システム導入後の電話件数推移", "tags": ["予約", "業務効率"],
        "summary": "Web予約導入から12ヶ月の電話問い合わせ件数推移を可視化。",
        "reuseHint": "クリニックDX案件の効果検証で再利用。",
    },
    {
        "industry": "製造", "proposalType": "競合比較", "graphType": "テーブル", "layoutType": "リスト",
        "title": "主要3社の生産能力比較", "tags": ["競合", "生産能力"],
        "summary": "自社と競合2社のライン数・能力・歩留まりを横並びで比較。",
        "reuseHint": "製造業の事業戦略提案で再利用。",
    },
    {
        "industry": "人材", "proposalType": "新規提案", "graphType": "なし", "layoutType": "左右比較",
        "title": "新評価制度の概念図", "tags": ["評価制度", "人事"],
        "summary": "旧制度と新制度のロジックを左右で比較し、変更ポイントを明示。",
        "reuseHint": "人事制度刷新提案のキースライド。",
    },
    {
        "industry": "EC", "proposalType": "ロードマップ", "graphType": "ロードマップ", "layoutType": "ロードマップ",
        "title": "EC基盤刷新ロードマップ", "tags": ["基盤刷新", "ロードマップ"],
        "summary": "現行ECからヘッドレス構成への移行を4四半期で完了させる工程。",
        "reuseHint": "EC基盤提案の全体像として再利用。",
    },
    {
        "industry": "小売", "proposalType": "新規提案", "graphType": "なし", "layoutType": "タイトル中央",
        "title": "新業態コンセプト", "tags": ["新業態", "コンセプト"],
        "summary": "都市型ミニフォーマット店舗のコンセプトとターゲットを定義。",
        "reuseHint": "新業態開発提案のオープニングで利用。",
    },
    {
        "industry": "金融", "proposalType": "効果検証", "graphType": "棒グラフ", "layoutType": "Before/After",
        "title": "アプリ刷新後のMAU変化", "tags": ["アプリ", "MAU"],
        "summary": "アプリ刷新前後3ヶ月のMAU推移を比較し、+22%を確認。",
        "reuseHint": "金融アプリ刷新案件の効果報告で再利用。",
    },
    {
        "industry": "広告", "proposalType": "現状分析", "graphType": "散布図", "layoutType": "4象限",
        "title": "媒体別CPA×ボリュームマップ", "tags": ["CPA", "媒体"],
        "summary": "主要広告媒体18個をCPAと獲得ボリュームで散布。",
        "reuseHint": "広告予算組み替え提案の現状分析で再利用。",
    },
]


def make_slides() -> list[dict]:
    base = datetime(2025, 1, 15, 9, 0, tzinfo=timezone.utc)
    slides = []
    files: dict[str, list[dict]] = {}
    for i, s in enumerate(SLIDES):
        ind = s["industry"]
        files.setdefault(ind, []).append(s)
        file_id = f"FILE-{ind}-{(len(files[ind]) - 1) // 4 + 1:02d}"
        file_name = f"{ind}_提案資料_{(len(files[ind]) - 1) // 4 + 1:02d}.pptx"
        page_no = ((len(files[ind]) - 1) % 4) + 3
        slide_id = f"slide-{i + 1:03d}"
        created = base + timedelta(days=i * 3)
        slides.append({
            "slideId": slide_id,
            "fileId": file_id,
            "fileName": file_name,
            "pageNo": page_no,
            "slideTitle": s["title"],
            "slideText": s["summary"],
            "industry": s["industry"],
            "proposalType": s["proposalType"],
            "graphType": s["graphType"],
            "layoutType": s["layoutType"],
            "tags": s["tags"],
            "summary": s["summary"],
            "reuseHint": s["reuseHint"],
            "thumbnailPath": f"/api/thumbnails/{slide_id}.svg",
            "sourceUrl": f"https://example.com/decks/{file_id}.pptx#slide={page_no}",
            "accessLevel": "internal",
            "createdAt": created.isoformat().replace("+00:00", "Z"),
            "updatedAt": created.isoformat().replace("+00:00", "Z"),
        })
    return slides


if __name__ == "__main__":
    out = Path(__file__).parent / "slides.json"
    slides = make_slides()
    out.write_text(json.dumps(slides, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(slides)} slides to {out}")
