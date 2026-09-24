# 判定アルゴリズム / How the detection works

CutSensei は動画を 0.1 秒ごとのフレームに区切り、音声と映像から特徴量を求めてから、
区間（説明・板書・待機・確認）に分類します。解析結果はプロジェクトに保存されるため、
設定を変えて再適用するときは解析をやり直さず、分類だけを数ミリ秒でやり直します。

```
音声 16 kHz ─┬─ Silero VAD（ニューラル）──────────┐
             ├─ 周期性（ピッチ 70–400 Hz）＋SNR ──┼─→ 発話らしさ
             └─ 高域エンベロープ → 打音（チョーク）─┼─→ 打音の頻度
映像 4 fps  ── 黒板範囲 → 線の出現・消失の追跡 ───┼─→ 板書らしさ
                             動き・人物の領域 ────┘      │
                                                         ↓
                                 分類（しきい値・ヒステリシス・区間の整形）
```

## 1. 音声

**発話**（`analysis/audio.py`）

- *Silero VAD*（MIT ライセンス、`models/silero_vad_16k.onnx`）が 32 ms ごとに発話確率を出します。
- 信号処理による検出：32 ms 窓の正規化自己相関で、声の高さ（70–400 Hz）に相当する周期性を調べます。
  1 kHz 以上の高い周期音（マーカーの「キュッ」）は、より短い周期にもピークが出ることで除外します
  （オクターブ判定）。周囲の雑音レベル（30 秒窓の 10 パーセンタイル）より 6 dB 以上大きいことも条件です。
- 両者を組み合わせ、近くに声の周期性がない単発の反応は弱めます。チョークの打音は周期性を持たないため、
  発話とは判定されません。

**打音**：1 ms ごとの高域エンベロープで、直前より 12 dB 以上急に立ち上がり 25 ms 以内に減衰する
短いパルスを数えます。無言区間での打音は板書の補助的な手がかりになります。

## 2. 映像（板書の検出）

`analysis/video.py` の `BoardTracker` が、黒板範囲（未指定なら画面全体）を 4 fps・幅 512 px 以下で処理します。

1. **線の抽出**：モルフォロジーのトップハット（黒板＝明るい線）／ブラックハット（ホワイトボード＝暗い線）で、
   細くコントラストの高い構造だけを取り出します。黒板かホワイトボードかは明るさから自動判定します。
2. **人物の領域**：ゆっくり更新する背景モデルとの差が大きい「大きな塊」を講師とみなし、その部分は観測しません。
   冒頭 30 秒の中央値で、講師がいない黒板の背景を推定します。立ち止まった講師は非常にゆっくりしか背景に
   取り込まれません。
3. **確定した変化**：人物に隠れておらず、動いてもいない画素で、線の有無が 0.75 秒以上安定してから、以前の
   確定状態と比べます。変化していれば「書いた（または消した）」とみなします。
   - しきい値にはヒステリシスがあり、圧縮ノイズやキーフレームごとの画質の揺れで線の縁がちらついても
     反応しません。孤立した数画素の変化は無視します。
   - その画素が隠れていた期間に書かれたとみなし、評価を「隠れ始めた直後」に重く配分します。
     講師の体の陰で書いた文字も、書いた時刻にさかのぼって板書と判定できます。
   - 12 秒以内に元に戻った変化（止めた手、指し示す腕、黒板の前での立ち止まり）は取り消します。
4. **動き**：前フレームとの差分から動きの量を求めます。動いているのに線が増えない＝歩いているだけ、です。
   照明の切り替えやカメラの揺れのような画面全体の急変はリセットとして扱います。

## 3. 分類

`analysis/classifier.py`

- **板書らしさ** = 線の変化量（2 秒平均）＋ 0.35 × 打音 ＋ 0.1 × 局所的な手の動き。
- 発話はヒステリシス付きのしきい値で判定し、息継ぎ程度の間（標準 0.85 秒）をつなぎ、0.2 秒未満の反応を捨て、
  前後に余白（0.25 秒／0.40 秒）を付けます。**発話は常に板書より優先**されます。
- 無言の区間：板書らしさがしきい値以上なら板書、しきい値付近（確認対象にする幅の中）なら「板書の可能性」、
  小さな発話らしさが残るなら「小さな発話の可能性」、無言で大きく動いているなら「無言での移動」を確認対象にします。
- 整形：板書中の短い間はつなぐ／短い板書は等速のまま／短い待機は削除しない／板書のあとは完成した板書を
  見せる時間を残す／削除の前後に少し余白を残す／倍速にはさまれた短い等速は倍速にまとめる。
- 再適用時は、手動で修正・保護した区間をそのまま残し、残りだけを新しい自動判定で埋めます。

「カットの積極性」は、削除する待機の最短時間（5.0 → 0.8 秒）、倍速にする板書の最短時間、つなぐ間の長さ、
板書後に残す時間、しきい値、確認対象にする幅などをまとめて動かします。詳細設定で個別に上書きできます。

## 4. 書き出し

`render/exporter.py`, `render/audio_render.py`

- **映像**：1 本の FFmpeg フィルタ（`select` → 区間ごとの一次関数による `setpts` → `fps`）で、カットと速度変更を
  フレーム単位で反映します。フィルタはファイル経由で渡すため、区間数に上限はありません。
- **音声**：Python 側でサンプル単位に組み立てます。倍速区間は WSOLA でピッチを保ったまま時間伸縮し、
  区間の長さは映像と同じ累積時刻から計算するので、長い講義でもずれが蓄積しません。つなぎ目には 4 ms の
  フェードを入れてクリック音を防ぎます。
- テスト（`tests/test_export.py`）では、フレーム番号を埋め込んだ映像と毎秒異なる周波数のトーンを使い、
  書き出し後の全フレームとトーンの位置が編集マップどおりであることを確認しています。

---

**English summary.** Audio: Silero VAD combined with a pitch/SNR detector; chalk taps are unvoiced
impulses and are counted separately. Video: thin strokes are extracted with a top-hat/black-hat
filter inside the board region; pixels hidden by the lecturer (large foreground blobs) are not
observed; a stroke counts once it is stable and differs from the last confirmed state (with
hysteresis, speck removal and retraction of changes undone within 12 s); the evidence is credited
back to the time the pixel was hidden. Classification uses hysteresis thresholds, speech margins,
gap filling, minimum durations, a hold after writing and review flags for ambiguous parts; manual
and protected segments survive regeneration. Export uses one FFmpeg `select`/`setpts`/`fps` graph
for video and WSOLA-rendered audio with sample-exact lengths.
