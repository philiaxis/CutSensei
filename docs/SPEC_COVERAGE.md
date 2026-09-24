# 設計書との対応表

「講義動画向け自動編集ソフト：簡易設計書」の各項目と、CutSensei の実装・検証箇所の対応です。

## 1. 目的

| 要件 | 実装 | 検証 |
|---|---|---|
| 説明中は等速、無言の板書は倍速、不要な待機はカット | `analysis/classifier.py` | `tests/test_classifier.py`, `tests/test_acceptance.py` |
| 自動編集後に一般的な編集ソフトに近い画面で確認・修正・書き出し | `ui/main_window.py` ほか | `tests/test_gui.py` |
| 固定カメラの講義動画一本を対象 | 黒板範囲・背景モデルが固定カメラ前提 | — |

## 2. 自動編集の仕様

| 要件 | 実装 | 検証 |
|---|---|---|
| 話している → 等速。板書と同時でも発話優先 | 分類時に発話ラベルが常に上書き | `test_speech_wins_over_writing` |
| 無言の板書 → n 倍速 | `Action.SPEED`、倍率は設定 | `test_basic_rules`, 受け入れテスト |
| 待機 → 削除し隙間なくつなぐ | `Action.CUT`、編集マップで詰める | `test_export_av_sync` |
| 曖昧・残す意味がありそう → 等速＋確認対象 | `Kind.UNCERTAIN` + `review=True` | `test_uncertain_is_kept_and_flagged` |
| 倍率は自由、初期値 4 倍 | `AutoEditSettings.writing_speed` | — |
| 倍速区間の音声：保持・音量調整・ミュート | `SpeedAudio`、WSOLA でピッチ維持 | `test_export_keeps_pitch_in_sped_up_part` |
| 無音判定だけで決めない／チョーク音を発話と区別 | Silero VAD＋声の周期性、打音は別集計 | `test_dsp_separates_speech_from_chalk` |
| 移動と書いている場面を区別 | 線の出現・消失の追跡、人物領域の除外 | `test_walking_person_is_not_writing` |
| 黒板・ホワイトボードの範囲を画面上で指定 | 黒板範囲ダイアログ（複数矩形） | `docs/images/board_region_ja.png` |
| 発話前後の保護余白 | `pad_before` / `pad_after` | `test_speech_margins` |
| 息継ぎ・問いかけ後の間・完成した式を見せる時間を切らない | 間のつなぎ、短い待機は残す、板書後の保持 | `test_breathing_gaps_stay_speech`, `test_short_pause_is_not_cut` |
| 等速と倍速の頻繁な切り替えを防ぐ | 最短倍速時間、板書中の間の統合、はさまれた等速の統合 | `test_short_writing_not_sped_up`, `test_writing_with_short_pauses_is_one_segment` |
| 通常表示：倍率・積極性・余白 | 右パネル「自動編集」 | — |
| 詳細設定：待機の最短時間、短い区間をまとめる条件など | 「詳細設定」（折りたたみ） | — |
| 設定変更後に再生成、手動修正・保護区間は上書きしない | `regenerate(..., keep_locked=True)` | `test_regenerate_preserves_manual_and_protected` |

## 3. 画面構成

| 位置 | 実装 |
|---|---|
| 上部ツールバー（プロジェクト名、読み込み、保存、元に戻す・やり直す、自動編集、書き出し） | `MainWindow._build_ui` |
| 左：素材・元の長さ・編集後の長さ・短縮率、タブで確認対象・削除区間 | `MediaPanel` |
| 中央：プレビュー、再生・停止、コマ送り、現在時刻、区間の種類と倍率 | `VideoView`, `TransportBar`, `PreviewEngine` |
| 右：自動編集の設定、選択区間の分類・速度・音量・保護（選択時は区間を優先表示） | `PropertiesPanel` |
| 下：サムネイル、音声波形、区間境界、再生位置、拡大縮小・スクロール・ドラッグ編集 | `TimelinePanel`, `TimelineView` |
| 各領域の幅・高さを調整、左右パネルの折りたたみ | `QSplitter`、ツールバーのパネル切替 |
| タイムラインは編集後の長さと速度を反映して表示 | 既定で「編集後の長さ」表示 |
| ダークな配色、色＋ラベル＋倍率、ツールチップ、キーボード操作 | `ui/theme.py`、全アクションにショートカット |

## 4. 手動編集と確認

| 要件 | 実装 | 検証 |
|---|---|---|
| 分割、開始・終了位置の調整、削除、隙間詰め、速度変更、音量調整 | S、境界ドラッグ、`[` / `]`、Delete、速度・音量 | `test_full_workflow`, `test_timeline_drag_boundary_and_pull_cut`, `test_set_edges_to_playhead` |
| 映像と音声を連動（音ズレ防止） | 同一の編集マップから映像・音声を生成 | `test_export_av_sync` |
| 等速／倍速／削除への変更、「必ず残す」保護 | 1 / 2 / 3、P | `test_full_workflow` |
| 削除区間の元映像確認と復元 | 「削除区間」タブ | — |
| 確認対象・境界へ順に移動し前後を再生 | N / ↑↓、A、移動後の自動再生 | `test_play_around_returns_to_playhead` |
| 非破壊、取り消し・やり直し | スナップショット方式の履歴 | `test_history_undo_redo`, GUI テスト |

## 5. 利用の流れ・保存・書き出し

| 要件 | 実装 | 検証 |
|---|---|---|
| 読み込み → 黒板範囲 → 設定 → 自動編集 → 修正 → 書き出し | ツールバーの並び・画面のガイド | — |
| 自動編集後に元の長さ・編集後の長さ・倍速/削除時間・確認対象数を表示 | ステータスバー、左パネル | — |
| 不確かな箇所は勝手に削除しない | 曖昧区間は等速＋確認対象 | 分類テスト |
| 解析・書き出しの進捗表示とキャンセル | `ProgressRunner`、`CancelToken` | `test_cancel_export` |
| プロジェクト保存（素材参照・判定結果・手動修正・設定） | `.cutsensei`（`docs/PROJECT_FORMAT.md`） | `test_project_roundtrip` |
| MP4、解像度・フレームレート・画質、初期値は元動画のまま | 書き出しダイアログ | `test_export_scaling_and_fps` |

## 6. 完成条件

| 要件 | 検証 |
|---|---|
| 読み込み〜自動判定〜確認・修正〜書き出しを一通り完了 | `test_full_workflow`、配布版 CI のスモークテスト |
| 最終動画へ速度変更とカットが反映 | `test_export_av_sync`（全フレーム・音声位置を照合） |
| 話しながらの板書は等速、無言の板書は倍速、発話の頭と末尾が欠けない | `tests/test_acceptance.py`, `test_speech_margins` |
| 説明 60 秒＋無言の板書 40 秒＋待機 20 秒（4 倍）→ 余白を除き 70 秒 | `test_design_doc_scenario`（積極性 0/50/100 で 70〜75 秒） |

## 対応環境

Windows / macOS（Apple Silicon・Intel）/ Linux。GitHub Actions で 3 OS × Python 3.10 / 3.12 の全テストと、
4 種類の配布版（ビルド → 同梱 CLI でデモ動画を解析・書き出し → GUI 起動）を検証しています。
