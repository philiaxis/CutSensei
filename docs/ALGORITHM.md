# 判定アルゴリズム / How the detection works

CutSensei は動画を 0.1 秒ごとのフレームに区切り、音声と映像から特徴量を求めてから、
区間（説明・板書・待機・確認）に分類します。解析結果はプロジェクトに保存されるため、
設定を変えて再適用するときは解析をやり直さず、分類だけを数ミリ秒でやり直します。

```
音声 16 kHz ─┬─ Silero VAD（ニューラル）──────────┐
             ├─ 周期性（ピッチ 70–400 Hz）＋SNR ──┼─→ 発話らしさ
             └─ 高域エンベロープ → 打音（チョーク）─┼─→ 打音の頻度
映像 4 fps  ── 素材の種類の判定（カメラ／画面録画）   │
             ├─ カメラ：板書範囲 → 線の出現・消失 ───┼─→ 板書らしさ
             │           動き・人物の領域 ───────────┤
             └─ 画面録画：残る画素の変化・ページ操作 ┘      │
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

## 2b. 画面録画（電子黒板・電子ノート）

**素材の種類の判定**（`analysis/source.py`）：動画全体から 10 か所ほど、0.5 秒離れた 2 フレームを
最近傍縮小で取り出し（画素の統計をぼかさないため）、次の特徴を調べます。

- 画面録画は、紙やアプリの背景など **ちょうど同じ色** が画面の大半を占め（上位 5 色の割合が高い）、
  完全に平らな部分が多く、何も書いていなければ 2 フレームがまったく同じです。
- カメラ映像は、照明のむらと質感で色が多くの値に散らばり、細かなノイズがどこにでもあり、フレームごとに
  揺らぎます（強く圧縮しても同様。実写のサンプル映像でも確認しています）。

画面録画のうち、質感があって **ほぼ毎回変化している** 部分（講師のカメラ映像・再生中の動画）は
矩形として記録し、解析から除外します。判定があいまいな場合はカメラ撮影として扱います。

**書き込みの検出**（`analysis/screen.py` の `ScreenTracker`）：4 fps・長辺 960 px 以下のカラー画像で処理します。
画面録画にはノイズも講師の体もないため、画素の変化そのものを扱えます。

1. 基準のページ画像と比べて変化し、その後 **1.5 秒間変化しなかった** 画素を「書き込み（または消去）」として
   確定し、変化が始まった時刻に記録します。レーザーポインターの軌跡、カーソル、ペンのホバー、点滅する
   カーソルは 1.5 秒残らないので数えません。12 秒以内に元の色へ戻った変化（止めたレーザーの点、開いていた
   メニュー）は取り消します。
2. しきい値はページの質感に合わせて変えます。写真やグラデーションの上ではキーフレームごとの圧縮の揺れが
   大きいため、無地の紙の上より大きな変化だけを数えます。
3. 量は 4×4 画素のセル数で数えるので、ペンの太さではなく書いた線の長さにほぼ比例します。
4. **ページ操作**：画面の広い範囲が一度に変わる、画像全体の平行移動で説明できる（位相相関で検出するスクロール・
   パン）、ページ全体の多数の箇所が同時に変わる（ページめくり、ページの消去）場合は「ページ操作」とし、
   落ち着いた後の画面を新しい基準にします。前のページの内容を新しい板書として数えることはありません。
   数秒以上続く変化（動画の再生など）はページ操作ではなく「動き」として扱います。
5. **表示の変化の除外**：前後 4 秒にほかの変化がない小さな変化が同じ場所で繰り返される場合（ステータスバーの
   時計・電池表示）は、その場所の変化をすべて除きます。孤立したごく小さな変化も無視します。
6. **カメラ映像の除外（予備）**：事前の判定で見つからなかった場合も、8 秒以上ほぼ毎フレーム変化し続ける
   領域はその場で除外し、それまでにそこで数えた分も取り消します。

分類では、板書らしさ = 書き込み量 ＋ 0.8 × ページ操作 ＋ 0.15 × 打音（ペン先や打鍵の音は弱い手がかり）とします。
無言のページめくりは短く等速で見せ、無言でページを行き来している間は倍速になります。無言でレーザーポインターを
動かし続けている間は「無言の動き」として等速で残し、確認対象にします（マウスカーソル程度の小さな動きは除く）。

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

**Screen recordings.** `analysis/source.py` samples frame pairs across the video with
nearest-neighbour scaling: screen content is dominated by a few exact colours, has exactly flat areas
and does not change between frames; camera images spread over many values (lighting, texture) and
carry noise. Textured areas of a screen recording that change in nearly every sampled pair (a webcam
picture, a playing video) are masked. `analysis/screen.py` then counts pixels that changed and stayed
unchanged for 1.5 s (laser pointer, cursor and menus do not), credited to the moment they changed and
measured in 4x4 cells (stroke length rather than pen width), with a texture-dependent threshold
against key-frame flicker, retraction of changes undone within 12 s, removal of small isolated
changes that repeat at one place (a clock), and a run-time mask for areas that keep changing.
Scrolling (phase correlation), zooming and page turns are reported as navigation and the settled page
becomes the new reference. Classification uses ink + 0.8 x navigation + 0.15 x taps; silent pointer
movement is kept and marked for checking.
