#!/usr/bin/env python3
"""
Lucha Pinchadiscos (LP) — single deck, KORG nanoKONTROL2 support
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import numpy as np
import sounddevice as sd
import soundfile as sf
import rtmidi
import miniaudio
from tkinterdnd2 import TkinterDnD, DND_FILES
import os
import time
import json
import math

# ── MIDI CC マッピング (nanoKONTROL2 デフォルト) ──────────────────
CC_VOLUME        = 0    # Fader 1
CC_TEMPO         = 16   # Knob 1  (±50% センター=64)
CC_SCRATCH       = 17   # Knob 2  (細かいスクラッチ: delta × 0.05秒)
CC_SCRATCH_WIDE  = 18   # Knob 3  (ワイドスクラッチ 小: jump×0.12 / 1秒タイムアウト)
CC_SCRATCH_XWIDE = 19   # Knob 4  (ワイドスクラッチ 大: jump×0.4  / 1秒タイムアウト)
CC_PLAY          = 41   # Transport Play  (toggle)
CC_STOP          = 42   # Transport Stop  (先頭に戻す)
CC_CUE           = 43   # REV (逆再生トグル)
CC_RECORD        = 45   # Transport Rec  (押しっぱなしでグリッチループ)
CC_LOOP          = 46   # Cycle button  (IN/OUTループ: 1押IN→2押OUT+LOOP→3押解除)
CC_SLOT          = [32, 33, 34, 35, 36, 37, 38, 39]  # Solo 1-8: トラックスロット選択

SAMPLE_RATE = 44100

# ── トラックスロット ───────────────────────────────────────────────
NUM_SLOTS   = 8
track_slots = [None] * NUM_SLOTS   # 各要素: {'data': ndarray, 'name': str, 'dur': float}
active_slot = 0                    # 現在選択中のスロット番号
undo_stack  = []                   # [(track_slots snapshot, active_slot), ...]
UNDO_MAX    = 20

# ── 状態 ──────────────────────────────────────────────────────────
audio_data   = None   # np.ndarray shape=(N, ch)
audio_ch     = 2
pos          = 0.0    # 再生位置（サンプル）
playing      = False
volume       = 1.0
speed        = 1.0    # 1.0 = normal
reverse      = False  # 逆再生フラグ
looping      = False
loop_start   = 0.0
loop_end_    = 0.0    # 末尾まで

# ── マーカーポイント（サンプル単位、0=未設定） ────────────────────
track_start  = 0.0   # 再生開始位置
track_end    = 0.0   # 再生終了位置（0=トラック末尾）
loop_in_pt   = 0.0   # ループIN
loop_out_pt  = 0.0   # ループOUT（0=未設定）

scratch_prev           = None   # 前回のスクラッチノブ値 (Knob2)
scratch_wide_prev      = None   # 前回のワイドスクラッチノブ値 (Knob3)
scratch_wide_last_time = 0.0
scratch_wide_locked    = False
scratch_vel            = 0.0    # Knob3 慣性速度
scratch_xwide_prev      = None  # 前回のワイドスクラッチノブ値 (Knob4)
scratch_xwide_last_time = 0.0
scratch_xwide_locked    = False
scratch_xvel            = 0.0   # Knob4 慣性速度
scratch_active    = False

vinyl_stopping    = False
speed_before_stop = 1.0

loop_rec_phase    = 0    # 0=off  1=IN点確定済み  2=ループ中

# pos と speed を同じロックで守る
state_lock = threading.Lock()

# ── オーディオコールバック ─────────────────────────────────────────
# ロックを使わない設計: CPython の GIL により float/bool の単純な
# 読み書きは原子的。ロックをコールバック内で取ると他スレッドが
# 保持中にブロックしてアンダーランが発生するため除去する。
def audio_callback(outdata, frames, time_info, status):
    global pos, playing, scratch_vel, scratch_xvel

    _data = audio_data
    _svel = scratch_vel + scratch_xvel

    # スクラッチ中は停止中でも音を出す（絶対速度が閾値以上なら有効）
    _scratching = abs(_svel) > 0.05
    _playing    = playing or _scratching

    if not _playing or _data is None:
        outdata[:] = 0
        return

    # 慣性速度の減衰は常に行う（停止中スクラッチでも自然に止まる）
    scratch_vel  *= 0.88
    scratch_xvel *= 0.88

    _pos  = pos
    # 停止中スクラッチ時は speed=0 として慣性のみ使用
    _base = (-(speed) if reverse else speed) if playing else 0.0
    _spd  = _base + _svel
    _vol    = volume
    _loop   = looping
    _lstart = loop_start
    _lend   = loop_end_
    total   = len(_data)
    # START/END マーカーで再生範囲を制限
    _tstart = int(track_start) if track_start > 0 else 0
    _tend   = int(track_end)   if track_end   > 0 else total

    if _loop:
        end   = int(_lend) if _lend > 0 else _tend
        start = int(_lstart)
    else:
        end   = _tend
        start = _tstart

    # pos がSTART前に出た場合、即座にSTARTに戻す（無音・誤再生を防ぐ）
    if not _loop and _pos < start:
        _pos = float(start)
        pos  = _pos

    indices = _pos + np.arange(frames, dtype=np.float64) * _spd

    if _loop:
        loop_len = end - start
        if loop_len > 0:
            indices = start + np.mod(indices - start, loop_len)
    elif _spd >= 0:
        # 順方向: ENDを超えたら停止
        beyond = indices >= end
        if beyond.any():
            indices[int(np.argmax(beyond)):] = end - 1
            playing = False
    else:
        # 逆方向スピード: STARTに当たったとき
        beyond = indices <= start
        if beyond.any():
            if reverse:
                # 逆再生モードなら停止（STARTが終端）
                indices[:int(np.argmax(beyond)) + 1] = start
                playing = False
            else:
                # スクラッチ慣性によるSTART衝突 → 慣性リセットしてSTARTから継続
                indices[beyond] = float(start)
                scratch_vel  = 0.0
                scratch_xvel = 0.0
                pos = float(start)

    indices = np.clip(indices, 0, total - 2)
    i0   = indices.astype(np.int64)
    frac = (indices - i0).astype(np.float32)[:, np.newaxis]

    buf = _data[i0] * (1.0 - frac) + _data[i0 + 1] * frac
    pos = float(indices[-1] + _spd)  # 原子的書き込み

    # クリッピング防止（歪み対策）
    np.clip(buf * _vol, -1.0, 1.0, out=buf)
    outdata[:] = buf.astype(np.float32)


BLOCKSIZE = 512
_current_device = None   # None = デフォルトデバイス

def _start_stream(device=None):
    global stream, _current_device
    try:
        stream.stop()
        stream.close()
    except Exception:
        pass
    _current_device = device
    stream = sd.OutputStream(
        device=device,
        samplerate=SAMPLE_RATE,
        channels=2,
        dtype='float32',
        blocksize=BLOCKSIZE,
        latency='low',
        callback=audio_callback,
    )
    stream.start()

stream = None
_start_stream(None)
pos_lock = state_lock

# ── オーディオロード (miniaudio: ffmpeg不要) ──────────────────────
def load_mp3(path):
    global audio_data, audio_ch, pos, loop_end_
    decoded = miniaudio.decode_file(
        path,
        output_format=miniaudio.SampleFormat.FLOAT32,
        nchannels=2,
        sample_rate=SAMPLE_RATE,
    )
    data = np.frombuffer(decoded.samples, dtype=np.float32).reshape(-1, 2)
    audio_data = data
    audio_ch   = 2
    pos        = 0.0
    loop_end_  = 0.0
    return len(data) / SAMPLE_RATE  # 秒





# ── GUI ───────────────────────────────────────────────────────────
class DJApp(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title("Lucha Pinchadiscos  LP")
        self.configure(bg="#1a1a2e")
        self.resizable(False, False)
        self._duration = 0.0
        self._filename = ""
        self._zoom        = 1.0
        self._zoom_center = 0.5
        # スロットドラッグ状態
        self._drag_slot_src   = None   # ドラッグ元スロット番号
        self._drag_slot_over  = None   # ホバー中スロット番号
        self._drag_slot_moved = False  # 移動したかどうか（クリックと区別）
        self._build_menu()
        self._build_ui()
        self._update_loop()

        # MIDI
        self._midi_in = None
        self._open_midi()

        # キーバインド
        self.bind("<Command-z>", lambda e: self._undo())
        self.bind("<Command-s>", lambda e: self._save_project())
        self.bind("<Command-o>", lambda e: self._load_project())
        self.bind("<space>",     lambda e: self._play_stop())
        self.bind("<Left>",      lambda e: self._seek_rel(-1.0))
        self.bind("<Right>",     lambda e: self._seek_rel(1.0))
        self.bind("<Shift-Left>",  lambda e: self._seek_rel(-5.0))
        self.bind("<Shift-Right>", lambda e: self._seek_rel(5.0))
        self.bind("r", lambda e: self._toggle_reverse())
        self.bind("R", lambda e: self._toggle_reverse())
        for _i in range(NUM_SLOTS):
            self.bind(str(_i + 1), lambda e, n=_i: self._select_slot(n))

    def _build_menu(self):
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Save Project",
                              accelerator="Cmd+S",
                              command=self._save_project)
        file_menu.add_command(label="Open Project",
                              accelerator="Cmd+O",
                              command=self._load_project)
        file_menu.add_separator()
        file_menu.add_command(label="Quit",
                              accelerator="Cmd+Q",
                              command=self.on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        settings_menu = tk.Menu(menubar, tearoff=0)
        settings_menu.add_command(label="Audio Output Device…",
                                  command=self._open_device_dialog)
        menubar.add_cascade(label="Settings", menu=settings_menu)

        self.config(menu=menubar)
        self.bind("<Command-q>", lambda e: self.on_close())

    # ── UI構築 ────────────────────────────────────────────────────
    def _build_ui(self):
        PAD = 12
        BG  = "#1a1a2e"
        FG  = "#e0e0e0"
        ACC = "#e94560"
        BTN = "#16213e"

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TScale", background=BG, troughcolor="#2a2a4e",
                        sliderthickness=18)

        outer = tk.Frame(self, bg=BG, padx=PAD, pady=PAD)
        outer.pack()

        # タイトル
        tk.Label(outer, text="KID  A", font=("Courier", 22, "bold"),
                 bg=BG, fg=ACC).pack()

        # ファイル名
        self.lbl_file = tk.Label(outer, text="— no file —",
                                  font=("Courier", 11), bg=BG, fg=FG,
                                  width=44, anchor="w")
        self.lbl_file.pack(pady=(4, 0))

        # 波形エリア（上段: RESETボタン右上、下段: キャンバス、その下: W LOOP）
        def _wlbtn(parent, text, cmd, fg, bg, bg_press, w=8):
            lbl = tk.Label(parent, text=text, font=("Courier", 10, "bold"),
                           bg=bg, fg=fg, relief="groove", bd=2,
                           width=w, height=1, cursor="hand2")
            lbl.bind("<ButtonPress-1>",   lambda e: lbl.config(bg=bg_press))
            lbl.bind("<ButtonRelease-1>", lambda e: (lbl.config(bg=bg), cmd()))
            return lbl

        # キャンバス上段: RESETを右上、ズームボタンを左上に配置
        row_wtop = tk.Frame(outer, bg=BG)
        row_wtop.pack(fill="x", pady=(6, 0))
        _wlbtn(row_wtop, "RESET", self._reset_markers,
               fg="#ff6680", bg="#2a0a10", bg_press="#5a1030", w=6).pack(side="right")
        _wlbtn(row_wtop, "+", lambda: self._zoom_by(1.5, 250),
               fg="#aaffaa", bg="#0a2a0a", bg_press="#1a5a1a", w=3).pack(side="left")
        _wlbtn(row_wtop, "-", lambda: self._zoom_by(1/1.5, 250),
               fg="#ffaaaa", bg="#2a0a0a", bg_press="#5a1a1a", w=3).pack(side="left")
        self.lbl_zoom = tk.Label(row_wtop, text="1x", font=("Courier", 10),
                                  bg=BG, fg="#aaaaaa")
        self.lbl_zoom.pack(side="left", padx=4)

        # 波形 / 位置バー（D&D受付）
        self.canvas = tk.Canvas(outer, width=500, height=80,
                                 bg="#0d0d1a", highlightthickness=1,
                                 highlightbackground="#2a2a4e")
        self.canvas.pack(pady=(0, 0))
        self._drag_last_x    = None
        self._dragging_marker = None   # None | 'loop_in' | 'loop_out'
        self.canvas.bind("<ButtonPress-1>",   self._drag_start)
        self.canvas.bind("<B1-Motion>",        self._drag_motion)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Motion>",           self._canvas_hover)
        self.canvas.bind("<ButtonPress-2>",   self._canvas_ctx)
        self.canvas.bind("<ButtonPress-3>",   self._canvas_ctx)
        self.canvas.bind("<MouseWheel>", lambda e: self._zoom_by(1.5 if e.delta > 0 else 1/1.5, e.x))
        self.canvas.bind("<Button-4>",   lambda e: self._zoom_by(1.5, e.x))
        self.canvas.bind("<Button-5>",   lambda e: self._zoom_by(1/1.5, e.x))
        self.canvas.drop_target_register(DND_FILES)
        self.canvas.dnd_bind("<<Drop>>", self._on_drop)
        self.canvas.dnd_bind("<<DragEnter>>", self._on_drag_enter)
        self.canvas.dnd_bind("<<DragLeave>>", self._on_drag_leave)

        # キャンバス下段: W LOOPを右下に配置
        row_wbot = tk.Frame(outer, bg=BG)
        row_wbot.pack(fill="x", pady=(0, 4))
        self.btn_wloop = _wlbtn(row_wbot, "W LOOP", self._w_loop,
                                 fg="#ffdd44", bg="#1e2a4a", bg_press="#4a3a00")
        self.btn_wloop.pack(side="right")

        # 時間表示
        row_time = tk.Frame(outer, bg=BG)
        row_time.pack(fill="x")
        self.lbl_pos  = tk.Label(row_time, text="0:00.0", font=("Courier", 14),
                                  bg=BG, fg=ACC)
        self.lbl_pos.pack(side="left")
        self.lbl_dur  = tk.Label(row_time, text="/ 0:00.0",
                                  font=("Courier", 12), bg=BG, fg=FG)
        self.lbl_dur.pack(side="left", padx=4)

        # テンポ
        tk.Label(outer, text="TEMPO  %", font=("Courier", 10),
                 bg=BG, fg=FG).pack(pady=(8,0))
        self.lbl_tempo = tk.Label(outer, text="+0.0%",
                                   font=("Courier", 18, "bold"), bg=BG, fg=ACC)
        self.lbl_tempo.pack()

        # 手動スライダー: Tempo + リセットボタン
        row_tempo = tk.Frame(outer, bg=BG)
        row_tempo.pack(pady=2)
        self.tempo_var = tk.DoubleVar(value=0.0)  # ±50%
        self.sl_tempo = ttk.Scale(row_tempo, from_=-50, to=50,
                                   variable=self.tempo_var, orient="horizontal",
                                   length=360, command=self._on_tempo_slider)
        self.sl_tempo.pack(side="left")
        _btn_rst = tk.Label(row_tempo, text="±0", font=("Courier", 13, "bold"),
                            bg="#0a1e0a", fg="#aaffaa", relief="groove", bd=2,
                            width=4, height=2, cursor="hand2")
        _btn_rst.bind("<ButtonPress-1>",   lambda e: _btn_rst.config(bg="#1a3a1a"))
        _btn_rst.bind("<ButtonRelease-1>", lambda e: (_btn_rst.config(bg="#0a1e0a"), self._reset_tempo()))
        _btn_rst.pack(side="left", padx=(6, 0))

        # 手動スライダー: Volume
        tk.Label(outer, text="VOLUME", font=("Courier", 10),
                 bg=BG, fg=FG).pack(pady=(8,0))
        self.vol_var = tk.DoubleVar(value=100.0)
        self.sl_vol  = ttk.Scale(outer, from_=0, to=100,
                                  variable=self.vol_var, orient="horizontal",
                                  length=400, command=self._on_vol_slider)
        self.sl_vol.pack(pady=2)

        # トランスポートボタン
        # macOSのtkinterはtk.Buttonのbgを無視するため、tk.Labelでボタンを模倣する
        row_btn = tk.Frame(outer, bg=BG)
        row_btn.pack(pady=10)

        def _lbtn(parent, text, cmd, fg, bg, bg_press, w=9):
            lbl = tk.Label(parent, text=text, font=("Courier", 13, "bold"),
                           bg=bg, fg=fg, relief="groove", bd=2,
                           width=w, height=2, cursor="hand2")
            lbl.bind("<ButtonPress-1>",   lambda e: lbl.config(bg=bg_press))
            lbl.bind("<ButtonRelease-1>", lambda e: (lbl.config(bg=bg), cmd()))
            return lbl

        self.btn_play = _lbtn(row_btn, "▶  PLAY", self._play_stop,
                               fg="#ff3333", bg="#1e2a4a", bg_press="#5a0a0a")
        self.btn_play.pack(side="left", padx=5)

        _lbtn(row_btn, "■  STOP", self._stop,
              fg="#ffffff", bg="#111111", bg_press="#333333").pack(side="left", padx=5)

        self.btn_rev = _lbtn(row_btn, "◀◀  REV", self._toggle_reverse,
                              fg="#cc88ff", bg="#1e2a4a", bg_press="#3a1a5a")
        self.btn_rev.pack(side="left", padx=5)

        # IN/OUTループボタン（CYCLEに対応）
        self.btn_loop = _lbtn(row_btn, "↺ B LOOP", self._rec_loop,
                               fg="#ffdd44", bg="#1e2a4a", bg_press="#4a3a00")
        self.btn_loop.pack(side="left", padx=5)

        # グリッチボタン（RECに対応・押しっぱなし）
        self.btn_glitch = tk.Label(row_btn, text="●  GLITCH",
                                    font=("Courier", 13, "bold"),
                                    bg="#2a002a", fg="#ff44ff",
                                    relief="groove", bd=2,
                                    width=9, height=2, cursor="hand2")
        self.btn_glitch.bind("<ButtonPress-1>",   lambda e: self._glitch_on())
        self.btn_glitch.bind("<ButtonRelease-1>", lambda e: self._glitch_off())
        self.btn_glitch.pack(side="left", padx=5)

        # ── ターンテーブル ────────────────────────────────────────
        self._tt_angle          = 0.0
        self._tt_drag_angle     = None
        self._tt_scratch_active = False
        self._tt_braking        = False   # 手乗せブレーキ中フラグ
        self._tt_brake_speed_save = 1.0  # ブレーキ前の speed 退避
        self._tt_brake_id       = None   # ブレーキ減衰タイマー
        TT = 200
        self._tt_size = TT
        self._tt_canvas = tk.Canvas(outer, width=TT, height=TT,
                                     bg=BG, highlightthickness=0)
        self._tt_canvas.pack(pady=(6, 2))
        self._tt_cancel_id = None   # 非アクティブ化タイマー
        self._tt_canvas.bind("<MouseWheel>", self._tt_scroll)
        # 2本指クリック: 静止=ビニールブレーキ / 上下ドラッグ=スクラッチ
        self._tt_hand_last_y  = None   # ドラッグ追跡用
        self._tt_hand_moved   = False  # 動いたかどうか
        self._tt_canvas.bind("<ButtonPress-2>",   self._tt_hand_press)
        self._tt_canvas.bind("<B2-Motion>",       self._tt_hand_drag)
        self._tt_canvas.bind("<ButtonRelease-2>", self._tt_hand_release)
        self._tt_canvas.bind("<ButtonPress-3>",   self._tt_hand_press)
        self._tt_canvas.bind("<B3-Motion>",       self._tt_hand_drag)
        self._tt_canvas.bind("<ButtonRelease-3>", self._tt_hand_release)
        self._draw_turntable()

        # ── トラックスロットパネル ─────────────────────────────────
        slot_hdr = tk.Frame(outer, bg=BG)
        slot_hdr.pack(fill="x", pady=(10, 2))
        tk.Label(slot_hdr, text="TRACK SLOTS", font=("Courier", 10, "bold"),
                 bg=BG, fg=FG).pack(side="left")

        self.slot_frames = []
        self.slot_labels = []
        self.slot_btns   = []
        for i in range(NUM_SLOTS):
            row = tk.Frame(outer, bg=BG)
            row.pack(fill="x", pady=1)

            # スロット番号ボタン（選択）
            btn = tk.Button(row, text=f" {i+1} ", width=3,
                            font=("Courier", 11, "bold"),
                            bg="#16213e", fg="#888888",
                            activebackground="#2a2a5e", activeforeground=ACC,
                            relief="flat", bd=0,
                            command=lambda n=i: self._select_slot(n))
            btn.pack(side="left", padx=(0, 4))

            # ファイル名ラベル（クリックで選択、D&Dでロード）
            lbl = tk.Label(row, text="— empty —", font=("Courier", 10),
                           bg="#0d0d1a", fg="#666666", anchor="w",
                           width=42, relief="flat", cursor="hand2")
            lbl.pack(side="left", padx=2)
            lbl.bind("<ButtonPress-1>",   lambda e, n=i: self._slot_drag_start(e, n))
            lbl.bind("<B1-Motion>",       lambda e: self._slot_drag_motion(e))
            lbl.bind("<ButtonRelease-1>", lambda e: self._slot_drag_end(e))
            lbl.drop_target_register(DND_FILES)
            lbl.dnd_bind("<<Drop>>",      lambda e, n=i: self._on_slot_drop(e, n))
            lbl.dnd_bind("<<DragEnter>>", lambda e, n=i: self._on_slot_drag_enter(e, n))
            lbl.dnd_bind("<<DragLeave>>", lambda e, n=i: self._on_slot_drag_leave(e, n))

            # LOADボタン
            ld = tk.Button(row, text="LD", width=3,
                           font=("Courier", 10), bg="#16213e", fg="#88aaff",
                           activebackground="#2a2a5e", relief="flat", bd=0,
                           command=lambda n=i: self._load_slot(n))
            ld.pack(side="left", padx=2)

            # CLEARボタン
            cl = tk.Button(row, text="✕", width=2,
                           font=("Courier", 10), bg="#16213e", fg="#ff6680",
                           activebackground="#3a1020", relief="flat", bd=0,
                           command=lambda n=i: self._clear_slot(n))
            cl.pack(side="left", padx=2)

            self.slot_frames.append(row)
            self.slot_labels.append(lbl)
            self.slot_btns.append(btn)

        self._refresh_slot_ui()

        # MIDI ステータス
        self.lbl_midi = tk.Label(outer, text="MIDI: scanning…",
                                  font=("Courier", 10), bg=BG, fg="#888888")
        self.lbl_midi.pack(pady=(6, 0))

        # スクラッチ状態
        self.lbl_scratch = tk.Label(outer, text="SCRATCH: —",
                                     font=("Courier", 10), bg=BG, fg=FG)
        self.lbl_scratch.pack()

    # ── D&D ──────────────────────────────────────────────────────
    @staticmethod
    def _parse_paths(data):
        """tkinterdnd2 の "{path1} {path2}" 形式を複数パスに分解"""
        paths = []
        data  = data.strip()
        i = 0
        while i < len(data):
            if data[i] == '{':
                end = data.find('}', i)
                if end == -1:
                    break
                paths.append(data[i+1:end])
                i = end + 2
            else:
                end = data.find(' ', i)
                if end == -1:
                    paths.append(data[i:])
                    break
                paths.append(data[i:end])
                i = end + 1
        return [p for p in paths if p]

    def _on_drag_enter(self, event):
        self.canvas.config(highlightbackground="#e94560")

    def _on_drag_leave(self, event):
        self.canvas.config(highlightbackground="#2a2a4e")

    def _on_drop(self, event):
        self.canvas.config(highlightbackground="#2a2a4e")
        self._drop_paths_to_slots(self._parse_paths(event.data), active_slot)

    # ── ファイルロード ────────────────────────────────────────────
    def _load(self):
        path = filedialog.askopenfilename(
            title="MP3 を選択",
            filetypes=[("Audio files", "*.mp3 *.wav *.aiff *.flac"), ("All", "*.*")],
        )
        if not path:
            return
        self._load_path_to_slot(path, active_slot)

    # ── アンドゥ ─────────────────────────────────────────────────
    def _push_undo(self):
        global undo_stack
        snapshot = list(track_slots)   # スロットリストの浅いコピー
        undo_stack.append((snapshot, active_slot))
        if len(undo_stack) > UNDO_MAX:
            undo_stack.pop(0)

    def _undo(self):
        global track_slots, active_slot, audio_data, undo_stack
        if not undo_stack:
            return
        snapshot, prev_slot = undo_stack.pop()
        track_slots[:] = snapshot
        active_slot = prev_slot
        slot = track_slots[active_slot]
        if slot:
            audio_data     = slot['data']
            self._duration = slot['dur']
            self.lbl_file.config(text=slot['name'][:52])
            self.lbl_dur.config(text=f"/ {self._fmt(slot['dur'])}")
            self._draw_waveform()
        else:
            audio_data = None
            self.lbl_file.config(text="— no file —")
            self.lbl_dur.config(text="/ 0:00.0")
            self.canvas.delete("waveform")
            self.canvas.delete("playhead")
        self._refresh_slot_ui()

    # ── スロット操作 ──────────────────────────────────────────────
    def _refresh_slot_ui(self):
        for i in range(NUM_SLOTS):
            slot     = track_slots[i]
            is_active = (i == active_slot)
            is_src    = (i == self._drag_slot_src)
            is_over   = (i == self._drag_slot_over) and not is_src
            name  = slot['name'][:38] if slot else "— empty —"
            fg    = "#e0e0e0" if slot else "#666666"
            if is_over:                        # ドロップ先（紫ハイライト）
                bg_lbl = "#2a1040"
                btn_fg = "#dd88ff"
            elif is_src and self._drag_slot_moved:  # ドラッグ中（黄ハイライト）
                bg_lbl = "#2a2a00"
                btn_fg = "#ffff44"
            elif is_active:
                bg_lbl = "#1a2a1a"
                btn_fg = "#e94560"
            else:
                bg_lbl = "#0d0d1a"
                btn_fg = "#888888"
            self.slot_labels[i].config(text=name, fg=fg, bg=bg_lbl)
            self.slot_btns[i].config(fg=btn_fg)

    # ── スロット間ドラッグ＆ドロップ（入れ替え） ─────────────────────
    def _slot_drag_start(self, event, n):
        self._drag_slot_src    = n
        self._drag_slot_sx     = event.x_root
        self._drag_slot_sy     = event.y_root
        self._drag_slot_over   = None
        self._drag_slot_moved  = False

    def _slot_drag_motion(self, event):
        if self._drag_slot_src is None:
            return
        dx = abs(event.x_root - self._drag_slot_sx)
        dy = abs(event.y_root - self._drag_slot_sy)
        if dx < 5 and dy < 5 and not self._drag_slot_moved:
            return
        self._drag_slot_moved = True

        # カーソル下のスロットラベルを特定
        widget  = self.winfo_containing(event.x_root, event.y_root)
        new_over = None
        for i, lbl in enumerate(self.slot_labels):
            if widget is lbl:
                new_over = i
                break
        if new_over != self._drag_slot_over:
            self._drag_slot_over = new_over
            self._refresh_slot_ui()

    def _slot_drag_end(self, event):
        src = self._drag_slot_src
        tgt = self._drag_slot_over
        moved = self._drag_slot_moved
        # リセット（先にクリア）
        self._drag_slot_src   = None
        self._drag_slot_over  = None
        self._drag_slot_moved = False

        if not moved:
            # ドラッグなし → 通常クリック（選択）
            if src is not None:
                self._select_slot(src)
        elif tgt is not None and tgt != src:
            # ドラッグ有り → スロット入れ替え
            self._swap_slots(src, tgt)
        else:
            self._refresh_slot_ui()

    def _swap_slots(self, a, b):
        """スロット a と b の内容を入れ替える"""
        global active_slot
        self._push_undo()
        track_slots[a], track_slots[b] = track_slots[b], track_slots[a]
        # アクティブスロットが移動先に追従
        if active_slot == a:
            active_slot = b
        elif active_slot == b:
            active_slot = a
        self._select_slot(active_slot)

    def _select_slot(self, n):
        global active_slot, audio_data, pos, playing, looping, loop_end_
        global track_start, track_end, loop_in_pt, loop_out_pt
        active_slot = n
        slot = track_slots[n]
        if slot:
            audio_data   = slot['data']
            pos          = slot.get('start_pt', 0.0)
            looping      = False
            loop_end_    = 0.0
            track_start  = slot.get('start_pt',   0.0)
            track_end    = slot.get('end_pt',      0.0)
            loop_in_pt   = slot.get('loop_in_pt',  0.0)
            loop_out_pt  = slot.get('loop_out_pt', 0.0)
            self._duration = slot['dur']
            self.lbl_file.config(text=slot['name'][:52])
            self.lbl_dur.config(text=f"/ {self._fmt(slot['dur'])}")
            self._draw_waveform()
            self._refresh_markers()
        else:
            audio_data = None
            self.lbl_file.config(text="— no file —")
            self.lbl_dur.config(text="/ 0:00.0")
            self.canvas.delete("waveform")
        self._refresh_slot_ui()

    def _load_slot(self, n):
        path = filedialog.askopenfilename(
            title=f"スロット {n+1} に読み込む",
            filetypes=[("Audio files", "*.mp3 *.wav *.aiff *.flac"), ("All", "*.*")],
        )
        if path:
            self._load_path_to_slot(path, n)

    def _clear_slot(self, n):
        global audio_data, active_slot
        self._push_undo()
        track_slots[n] = None
        if active_slot == n:
            audio_data = None
            self.lbl_file.config(text="— no file —")
            self.lbl_dur.config(text="/ 0:00.0")
            self.canvas.delete("waveform")
            self.canvas.delete("playhead")
        self._refresh_slot_ui()

    def _on_slot_drop(self, event, n):
        self._drop_paths_to_slots(self._parse_paths(event.data), n)

    def _drop_paths_to_slots(self, paths, start_slot):
        """複数パスを start_slot から順にロード（1アンドゥ操作としてまとめる）"""
        if not paths:
            return
        self._push_undo()          # まとめて1回だけ保存
        slot = start_slot
        for path in paths[:NUM_SLOTS]:
            self._load_path_to_slot_no_undo(path, slot)
            slot = (slot + 1) % NUM_SLOTS

    def _load_path_to_slot_no_undo(self, path, n):
        """アンドゥ保存なしでスロットにロード（_drop_paths_to_slots 専用）"""
        global track_slots, audio_data, active_slot
        try:
            dur  = load_mp3(path)
            name = os.path.basename(path)
            track_slots[n] = {'data': audio_data, 'name': name, 'dur': dur, 'path': path}
            active_slot = n
            self._duration = dur
            self.lbl_file.config(text=name[:52])
            self.lbl_dur.config(text=f"/ {self._fmt(dur)}")
            self._draw_waveform()
            self._refresh_slot_ui()
        except Exception as e:
            messagebox.showerror("Load error", str(e))

    def _on_slot_drag_enter(self, event, n):
        self.slot_labels[n].config(bg="#1a1a3e")

    def _on_slot_drag_leave(self, event, n):
        is_active = (n == active_slot)
        self.slot_labels[n].config(bg="#1a2a1a" if is_active else "#0d0d1a")

    def _load_path_to_slot(self, path, n):
        global track_slots, audio_data, active_slot
        self._push_undo()
        try:
            dur  = load_mp3(path)
            name = os.path.basename(path)
            track_slots[n] = {'data': audio_data, 'name': name, 'dur': dur, 'path': path}
            # ロード直後はそのスロットをアクティブに
            active_slot = n
            self._duration = dur
            self.lbl_file.config(text=name[:52])
            self.lbl_dur.config(text=f"/ {self._fmt(dur)}")
            self._draw_waveform()
            self._refresh_slot_ui()
        except Exception as e:
            messagebox.showerror("Load error", str(e))

    def _load_path(self, path):
        try:
            dur = load_mp3(path)
            self._duration = dur
            self._filename = os.path.basename(path)
            self.lbl_file.config(text=self._filename[:52])
            self.lbl_dur.config(text=f"/ {self._fmt(dur)}")
            self._draw_waveform()
        except Exception as e:
            messagebox.showerror("Load error", str(e))

    def _delete_track(self):
        self._clear_slot(active_slot)
        self.canvas.delete("playhead")
        self.btn_play.config(text="▶ PLAY")

    # ── プロジェクト保存・読み込み ────────────────────────────────
    def _save_project(self):
        path = filedialog.asksaveasfilename(
            title="プロジェクトを保存",
            defaultextension=".djproj",
            filetypes=[("DJ Project", "*.djproj"), ("All", "*.*")],
        )
        if not path:
            return
        slots_data = []
        for slot in track_slots:
            if slot:
                slots_data.append({
                    'path':        slot.get('path', ''),
                    'name':        slot.get('name', ''),
                    'dur':         slot.get('dur', 0.0),
                    'start_pt':    slot.get('start_pt',    0.0),
                    'end_pt':      slot.get('end_pt',      0.0),
                    'loop_in_pt':  slot.get('loop_in_pt',  0.0),
                    'loop_out_pt': slot.get('loop_out_pt', 0.0),
                })
            else:
                slots_data.append(None)
        project = {
            'slots':       slots_data,
            'active_slot': active_slot,
            'volume':      volume,
            'speed':       speed,
        }
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(project, f, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showerror("Save error", str(e))

    def _load_project(self):
        path = filedialog.askopenfilename(
            title="プロジェクトを開く",
            filetypes=[("DJ Project", "*.djproj"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                project = json.load(f)
        except Exception as e:
            messagebox.showerror("Load error", str(e))
            return

        self._push_undo()
        global playing, volume, speed
        playing = False
        self.btn_play.config(text="▶ PLAY")

        slots_data = project.get('slots', [])
        errors = []
        for i, sd_entry in enumerate(slots_data[:NUM_SLOTS]):
            if sd_entry is None:
                track_slots[i] = None
                continue
            fpath = sd_entry.get('path', '')
            if not fpath or not os.path.exists(fpath):
                track_slots[i] = None
                if fpath:
                    errors.append(os.path.basename(fpath))
                continue
            try:
                dur = load_mp3(fpath)
                track_slots[i] = {
                    'data':        audio_data,
                    'name':        sd_entry.get('name', os.path.basename(fpath)),
                    'dur':         dur,
                    'path':        fpath,
                    'start_pt':    sd_entry.get('start_pt',    0.0),
                    'end_pt':      sd_entry.get('end_pt',      0.0),
                    'loop_in_pt':  sd_entry.get('loop_in_pt',  0.0),
                    'loop_out_pt': sd_entry.get('loop_out_pt', 0.0),
                }
            except Exception as e:
                track_slots[i] = None
                errors.append(os.path.basename(fpath))

        volume = project.get('volume', 1.0)
        speed  = project.get('speed',  1.0)
        self.vol_var.config   if False else None
        self.vol_var.set(volume * 100)
        pct = (speed - 1.0) * 100
        self.tempo_var.set(pct)
        self.lbl_tempo.config(text=f"{pct:+.1f}%")

        slot_n = project.get('active_slot', 0)
        self._select_slot(slot_n)
        self._refresh_slot_ui()

        if errors:
            messagebox.showwarning("一部読み込めませんでした",
                                   "見つからないファイル:\n" + "\n".join(errors))

    # ── マーカー操作 ──────────────────────────────────────────────
    def _set_start(self):
        global track_start
        track_start = pos
        self._save_markers()
        self._refresh_markers()

    def _set_end(self):
        global track_end
        track_end = pos
        self._save_markers()
        self._refresh_markers()

    def _set_loop_in(self):
        global loop_in_pt, loop_start
        loop_in_pt = pos
        loop_start = pos
        self._save_markers()
        self._refresh_markers()

    def _set_loop_out(self):
        global loop_out_pt, loop_end_
        loop_out_pt = pos
        loop_end_   = pos
        self._save_markers()
        self._refresh_markers()

    def _reset_markers(self):
        global track_start, track_end, loop_in_pt, loop_out_pt, loop_start, loop_end_
        track_start = 0.0
        track_end   = 0.0
        loop_in_pt  = 0.0
        loop_out_pt = 0.0
        loop_start  = 0.0
        loop_end_   = 0.0
        self._save_markers()
        self._refresh_markers()

    def _save_markers(self):
        slot = track_slots[active_slot]
        if slot:
            slot['start_pt']   = track_start
            slot['end_pt']     = track_end
            slot['loop_in_pt'] = loop_in_pt
            slot['loop_out_pt']= loop_out_pt

    def _refresh_markers(self):
        self._draw_waveform()

    def _draw_waveform(self):
        global audio_data
        if audio_data is None:
            return
        W, H = 500, 80
        total = len(audio_data)
        self.canvas.delete("waveform")
        mid = H // 2
        # ズーム範囲のサンプルだけ描画
        if self._zoom <= 1.0:
            v_start = 0
            v_end   = total
        else:
            half    = 0.5 / self._zoom
            v_start = int(max(0.0, self._zoom_center - half) * total)
            v_end   = int(min(1.0, self._zoom_center + half) * total)
        seg_len = max(1, v_end - v_start)
        step    = max(1, seg_len // W)
        mono    = audio_data[v_start:v_end:step, 0][:W]
        for x, s in enumerate(mono):
            h = int(abs(s) * mid)
            self.canvas.create_line(x, mid - h, x, mid + h,
                                     fill="#2a5caa", tags="waveform")
        # マーカー線を描画
        if track_start > 0:
            x = self._sample_to_cx(track_start)
            self.canvas.create_line(x, 0, x, H, fill="#44cc88", width=2, tags="waveform")
        if track_end > 0:
            x = self._sample_to_cx(track_end)
            self.canvas.create_line(x, 0, x, H, fill="#e94560", width=2, tags="waveform")
        if loop_in_pt > 0:
            x = self._sample_to_cx(loop_in_pt)
            self.canvas.create_line(x, 0, x, H, fill="#ffcc00", width=2, dash=(4, 3), tags="waveform")
        if loop_out_pt > 0:
            x = self._sample_to_cx(loop_out_pt)
            self.canvas.create_line(x, 0, x, H, fill="#88aaff", width=2, dash=(4, 3), tags="waveform")

    # ── マーカーヒットテスト（全マーカー、8px許容） ──────────────────
    def _marker_hit(self, x):
        if audio_data is None:
            return None
        THRESH = 8
        checks = [
            (loop_in_pt,  'loop_in'),
            (loop_out_pt, 'loop_out'),
            (track_start, 'track_start'),
            (track_end,   'track_end'),
        ]
        for pt, name in checks:
            if pt > 0:
                if abs(x - self._sample_to_cx(pt)) <= THRESH:
                    return name
        return None

    # ── ズームヘルパー ────────────────────────────────────────────
    def _cx_to_sample(self, x):
        """canvas x → audio sample"""
        if audio_data is None:
            return 0.0
        total = float(len(audio_data))
        if self._zoom <= 1.0:
            return x / 500 * total
        vstart = self._zoom_center - 0.5 / self._zoom
        vend   = self._zoom_center + 0.5 / self._zoom
        return (vstart + x / 500 * (vend - vstart)) * total

    def _sample_to_cx(self, s):
        """audio sample → canvas x"""
        if audio_data is None:
            return 0
        total = float(len(audio_data))
        frac = s / total
        if self._zoom <= 1.0:
            return int(frac * 500)
        vstart = self._zoom_center - 0.5 / self._zoom
        vend   = self._zoom_center + 0.5 / self._zoom
        return int((frac - vstart) / max(vend - vstart, 1e-9) * 500)

    def _zoom_by(self, factor, cx=250):
        """factor>1でズームイン、<1でズームアウト。cx=canvas x中心"""
        if audio_data is None:
            return
        # ズーム前のcx位置のfraction
        frac = self._cx_to_sample(cx) / len(audio_data)
        new_zoom = max(1.0, min(16.0, self._zoom * factor))
        self._zoom = new_zoom
        # cxがfrac位置に来るようにcenterを調整
        half = 0.5 / self._zoom
        self._zoom_center = max(half, min(1.0 - half, frac))
        self.lbl_zoom.config(text=f"{int(self._zoom)}x" if self._zoom >= 1 else "1x")
        self._draw_waveform()

    def _seek_rel(self, secs):
        global pos
        if audio_data is None:
            return
        lo  = float(track_start)
        pos = max(lo, min(pos + secs * SAMPLE_RATE, float(len(audio_data) - 1)))

    def _canvas_hover(self, event):
        """マーカー付近でカーソルを左右矢印に変える"""
        if self._dragging_marker or self._drag_last_x is not None:
            return
        hit = self._marker_hit(event.x)
        self.canvas.config(cursor="sb_h_double_arrow" if hit else "")

    def _drag_start(self, event):
        global pos, scratch_vel
        if audio_data is None:
            return
        hit = self._marker_hit(event.x)
        if hit:
            # マーカードラッグモード
            self._dragging_marker = hit
            self.canvas.config(cursor="sb_h_double_arrow")
        else:
            # 通常スクラッチモード
            self._dragging_marker = None
            lo  = float(track_start)
            pos = max(lo, min(self._cx_to_sample(event.x), float(len(audio_data) - 1)))
            scratch_vel = 0.0
            self.canvas.config(cursor="fleur")
        self._drag_last_x = event.x

    def _drag_motion(self, event):
        global pos, scratch_vel, loop_in_pt, loop_out_pt, loop_start, loop_end_
        global track_start, track_end
        if audio_data is None or self._drag_last_x is None:
            return
        total  = float(len(audio_data))
        sample = max(0.0, min(self._cx_to_sample(event.x), total - 1))

        m = self._dragging_marker
        if m == 'loop_in':
            loop_in_pt = sample
            loop_start = sample
            self._save_markers()
            self._refresh_markers()
            self._activate_loop_if_ready()
        elif m == 'loop_out':
            loop_out_pt = sample
            loop_end_   = sample
            self._save_markers()
            self._refresh_markers()
            self._activate_loop_if_ready()
        elif m == 'track_start':
            track_start = sample
            self._save_markers()
            self._refresh_markers()
        elif m == 'track_end':
            track_end = sample
            self._save_markers()
            self._refresh_markers()
        else:
            delta_x = event.x - self._drag_last_x
            if delta_x == 0:
                self._drag_last_x = event.x
                return
            pos = max(float(track_start), min(sample, total - 1))
            scratch_vel = max(-12.0, min(12.0, scratch_vel + delta_x * 0.6))

        self._drag_last_x = event.x

    def _drag_end(self, event):
        self._dragging_marker = None
        self._drag_last_x     = None
        self.canvas.config(cursor="")

    def _canvas_ctx(self, event):
        """右クリックで波形上にコンテキストメニューを表示してマーカーを設定"""
        if audio_data is None:
            return
        click_sample = max(0.0, min(self._cx_to_sample(event.x), float(len(audio_data) - 1)))

        t = self._fmt(click_sample / SAMPLE_RATE)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=f"Set START here  ({t})",
                         command=lambda: self._set_marker_start(click_sample))
        menu.add_command(label=f"Set END here    ({t})",
                         command=lambda: self._set_marker_end(click_sample))
        menu.add_separator()
        menu.add_command(label=f"Set LOOP IN here  ({t})",
                         command=lambda: self._set_marker_loop_in(click_sample))
        menu.add_command(label=f"Set LOOP OUT here ({t})",
                         command=lambda: self._set_marker_loop_out(click_sample))
        menu.add_separator()
        menu.add_command(label="Reset markers", command=self._reset_markers)
        menu.tk_popup(event.x_root, event.y_root)

    def _set_marker_start(self, sample):
        global track_start
        track_start = sample
        self._save_markers()
        self._refresh_markers()

    def _set_marker_end(self, sample):
        global track_end
        track_end = sample
        self._save_markers()
        self._refresh_markers()

    def _set_marker_loop_in(self, sample):
        global loop_in_pt, loop_start
        loop_in_pt = sample
        loop_start = sample
        self._save_markers()
        self._refresh_markers()
        self._activate_loop_if_ready()

    def _set_marker_loop_out(self, sample):
        global loop_out_pt, loop_end_
        loop_out_pt = sample
        loop_end_   = sample
        self._save_markers()
        self._refresh_markers()
        self._activate_loop_if_ready()

    def _activate_loop_if_ready(self):
        """W LOOPマーカーが揃ったらW LOOPボタンを有効色に更新するだけ（自動ONはしない）"""
        ready = loop_in_pt > 0 and loop_out_pt > 0 and loop_in_pt < loop_out_pt
        self.after_idle(lambda: self.btn_wloop.config(fg="#44ee88" if ready else "#ffdd44"))

    def _w_loop(self):
        """波形ドラッグで設定したIN/OUTマーカーでループをON/OFF"""
        global looping, loop_start, loop_end_, loop_rec_phase
        if looping and loop_rec_phase == 0:
            # W LOOPがアクティブ → 解除
            looping = False
            self.btn_wloop.config(text="W LOOP", fg="#44ee88")
        elif loop_in_pt > 0 and loop_out_pt > 0 and loop_in_pt < loop_out_pt:
            # マーカーが設定済み → W LOOPをON
            loop_start     = loop_in_pt
            loop_end_      = loop_out_pt
            loop_rec_phase = 0   # B LOOPフェーズをリセット
            looping        = True
            self.btn_wloop.config(text="● W LOOP", fg="#ffffff")
            self.btn_loop.config(text="↺ B LOOP", fg="#ffdd44")
        else:
            # マーカー未設定
            self.btn_wloop.config(fg="#ff4444")

    # ── 再生制御 ──────────────────────────────────────────────────
    def _play_stop(self):
        global playing
        if audio_data is None:
            return
        # 一時停止→再開はカーソル位置そのままで再生
        playing = not playing
        self.btn_play.config(text="‖ PAUSE" if playing else "▶ PLAY")

    def _stop(self):
        global playing, pos, speed, vinyl_stopping, speed_before_stop
        if vinyl_stopping:
            return
        if playing:
            vinyl_stopping    = True
            with state_lock:
                speed_before_stop = speed
            threading.Thread(target=self._vinyl_brake, daemon=True).start()
        else:
            # 既に停止中: STARTマーカーに頭出し
            with state_lock:
                pos = float(track_start) if track_start > 0 else 0.0
            self.btn_play.config(text="▶ PLAY")

    def _vinyl_brake(self):
        global playing, speed, vinyl_stopping, speed_before_stop
        import math
        with state_lock:
            s_start = max(speed, 0.3)
        steps = 50
        for i in range(steps):
            t = i / steps
            new_speed = s_start * math.exp(-5.0 * t)
            with state_lock:
                speed = new_speed
            time.sleep(0.033)   # ~50fps
        # 完全停止: pos はそのまま（カーソル位置保持）。次のSTOPで頭出し
        with state_lock:
            speed   = speed_before_stop
            playing = False
        vinyl_stopping = False
        self.after_idle(lambda: self.btn_play.config(text="▶ PLAY"))

    def _toggle_reverse(self):
        global reverse
        reverse = not reverse
        if reverse:
            self.btn_rev.config(bg="#3a1a5a", fg="#ffffff")
        else:
            self.btn_rev.config(bg="#1e2a4a", fg="#cc88ff")

    # ── IN/OUTループ（CYCLEボタン） ───────────────────────────────
    def _rec_loop(self):
        global looping, loop_start, loop_end_, loop_rec_phase
        if audio_data is None:
            return
        if loop_rec_phase == 0:
            loop_start     = pos
            loop_rec_phase = 1
            self.btn_loop.config(text="● IN SET", fg="#ffcc00")
        elif loop_rec_phase == 1:
            out = pos if pos > loop_start + SAMPLE_RATE * 0.1 else loop_start + SAMPLE_RATE
            loop_end_      = out
            looping        = True
            loop_rec_phase = 2
            self.btn_loop.config(text="↺ B LOOP", fg="#44ee88")
        else:
            looping        = False
            loop_rec_phase = 0
            self.btn_loop.config(text="↺ B LOOP", fg="#ffdd44")

    # ── グリッチループ（RECボタン押しっぱなし） ───────────────────
    GLITCH_LEN = int(SAMPLE_RATE * 0.1)   # 100ms

    def _glitch_on(self):
        global looping, loop_start, loop_end_
        if audio_data is None:
            return
        loop_start = pos
        loop_end_  = pos + self.GLITCH_LEN
        looping    = True
        self.btn_glitch.config(fg="#ffffff", bg="#aa00aa")

    def _glitch_off(self):
        global looping
        looping = False
        self.btn_glitch.config(fg="#ff44ff", bg="#2a002a")

    # ── スライダー ────────────────────────────────────────────────
    def _on_tempo_slider(self, val=None):
        global speed
        pct   = self.tempo_var.get()
        speed = 1.0 + pct / 100.0
        self.lbl_tempo.config(text=f"{pct:+.1f}%")

    def _reset_tempo(self):
        global speed
        speed = 1.0
        self.tempo_var.set(0.0)
        self.lbl_tempo.config(text="+0.0%")

    def _on_vol_slider(self, val=None):
        global volume
        volume = self.vol_var.get() / 100.0

    # ── 表示更新ループ ────────────────────────────────────────────
    def _update_loop(self):
        global pos, playing, audio_data
        if audio_data is not None:
            p = pos
            t = p / SAMPLE_RATE
            self.lbl_pos.config(text=self._fmt(t))
            x = self._sample_to_cx(p)
            self.canvas.delete("playhead")
            self.canvas.create_line(x, 0, x, 80, fill="#e94560",
                                     width=2, tags="playhead")
        # ターンテーブル回転アニメーション（再生中のみ）
        if playing and not self._tt_scratch_active:
            # 80ms per frame, 1.5秒/revolution → 1rev = SAMPLE_RATE*1.5 samples
            self._tt_angle += 2 * math.pi * speed * 0.08 / 1.5
            self._tt_angle %= 2 * math.pi
            self._draw_turntable()
        self.after(80, self._update_loop)

    # ── ターンテーブル描画 ────────────────────────────────────────
    _TT_SAMPLES_PER_REV = int(SAMPLE_RATE * 1.5)  # 1回転 = 1.5秒分

    def _draw_turntable(self):
        c  = self._tt_canvas
        sz = self._tt_size
        cx = cy = sz // 2
        r  = sz // 2 - 3
        c.delete("all")

        # レコード本体
        c.create_oval(cx-r, cy-r, cx+r, cy+r,
                      fill="#111111", outline="#444444", width=2)

        # グルーヴ（溝）
        for gr in range(r - 6, 38, -7):
            shade = "#1c1c1c" if gr % 14 < 7 else "#181818"
            c.create_oval(cx-gr, cy-gr, cx+gr, cy+gr,
                          outline=shade, width=1)

        # センターラベル（回転する）― ライオンの顔
        lr = 36
        c.create_oval(cx-lr, cy-lr, cx+lr, cy+lr,
                      fill="#991111", outline="#cc2222", width=2)

        # ── ルチャドール・マスク（回転対応） ──
        a = self._tt_angle
        ca, sa = math.cos(a), math.sin(a)

        def R(dx, dy):
            return cx + dx * ca - dy * sa, cy + dx * sa + dy * ca

        def OP(odx, ody, rw, rh, n=24):
            pts = []
            for i in range(n):
                t = 2 * math.pi * i / n
                pts += list(R(odx + rw * math.cos(t), ody + rh * math.sin(t)))
            return pts

        # ── マスク本体（赤）──
        c.create_polygon(OP(0, 0, 20, 22),
                          fill="#cc1111", outline="#880000", width=2, smooth=True)

        # ── サイドパネル（青、左右）──
        c.create_polygon(*R(-20, -10), *R(-8, -18), *R(-8, 20), *R(-20, 12),
                          fill="#1144cc", outline="#0a2288", width=1)
        c.create_polygon(*R(20, -10),  *R(8, -18),  *R(8, 20),  *R(20, 12),
                          fill="#1144cc", outline="#0a2288", width=1)

        # ── 額の三角パネル（金）──
        c.create_polygon(*R(0, -22), *R(-10, -8), *R(10, -8),
                          fill="#ffcc00", outline="#cc8800", width=1)

        # ── 額の星 ──
        star_pts = []
        for i in range(10):
            ang_s = math.pi * 2 * i / 10 - math.pi / 2
            r_s = 6 if i % 2 == 0 else 3
            star_pts += list(R(r_s * math.cos(ang_s), -14 + r_s * math.sin(ang_s)))
        c.create_polygon(star_pts, fill="#cc1111", outline="#880000", width=1)

        # ── 目穴（黒縁＋白目＋黒目）──
        for edx in (-8, 8):
            # 菱形の目穴枠（金）
            c.create_polygon(*R(edx, -5), *R(edx-7, 0), *R(edx, 5), *R(edx+7, 0),
                              fill="#ffcc00", outline="#cc8800", width=1)
            # 白目
            c.create_polygon(OP(edx, 0, 5, 4),
                              fill="#eeeeee", outline="", smooth=True)
            # 黒目
            c.create_polygon(OP(edx, 0, 2, 2),
                              fill="#111111", outline="", smooth=True)

        # ── 鼻パネル（金の逆三角）──
        c.create_polygon(*R(0, 12), *R(-5, 4), *R(5, 4),
                          fill="#ffcc00", outline="#cc8800", width=1)

        # ── 口元ライン（金の横帯）──
        c.create_polygon(*R(-12, 14), *R(12, 14), *R(10, 18), *R(-10, 18),
                          fill="#ffcc00", outline="#cc8800", width=1)

        # ── 放射状の装飾ライン ──
        for ang_d in [math.pi*0.25, math.pi*0.75, math.pi*1.25, math.pi*1.75]:
            p1 = R(8 * math.cos(ang_d),  8 * math.sin(ang_d))
            p2 = R(19 * math.cos(ang_d), 19 * math.sin(ang_d))
            c.create_line(*p1, *p2, fill="#ffcc00", width=1)


        # 外リング: ブレーキ中=オレンジ / スクラッチ中=赤 / 通常=グレー
        if self._tt_braking:
            ring_color = "#ff8800"
        elif self._tt_scratch_active:
            ring_color = "#ff3333"
        else:
            ring_color = "#555555"
        c.create_oval(cx-r, cy-r, cx+r, cy+r,
                      outline=ring_color, width=3, fill="")

    def _tt_scroll(self, event):
        """2本指スワイプでスクラッチ（macOSトラックパッド <MouseWheel>）"""
        global pos, scratch_vel
        if audio_data is None:
            return

        # macOSのトラックパッドは滑らかなスクロールで細かいdeltaを連続送信
        # 上スワイプ(delta>0)=順方向、下スワイプ(delta<0)=逆方向
        raw = event.delta          # macOS: ±小数値の連続
        if raw == 0:
            return

        # 感度調整: 1回転分を大きなスワイプ1往復に相当させる
        factor = raw / 8.0
        total  = float(len(audio_data))
        jump   = factor * self._TT_SAMPLES_PER_REV / (2 * math.pi)
        pos    = max(float(track_start), min(pos + jump, total - 1))
        scratch_vel = max(-20.0, min(20.0, scratch_vel + factor * 0.8))

        # ターンテーブル視覚的に回転
        self._tt_angle = (self._tt_angle + factor * 0.15) % (2 * math.pi)

        # アクティブ表示（200ms後に自動解除）
        self._tt_scratch_active = True
        if self._tt_cancel_id:
            self.after_cancel(self._tt_cancel_id)
        self._tt_cancel_id = self.after(200, self._tt_deactivate)
        self._draw_turntable()

    def _tt_deactivate(self):
        self._tt_scratch_active = False
        self._tt_cancel_id      = None
        self._draw_turntable()

    def _tt_hand_press(self, event=None):
        """2本指クリック押下: ドラッグ追跡開始 + ブレーキ予約"""
        global speed
        if audio_data is None:
            return
        if self._tt_braking:
            return
        self._tt_hand_last_y      = event.y
        self._tt_hand_moved       = False
        self._tt_braking          = True
        self._tt_brake_speed_save = speed
        self._tt_brake_id = self.after(30, self._tt_brake_step)
        self._draw_turntable()

    def _tt_hand_drag(self, event=None):
        """2本指ドラッグ中: ブレーキ減衰中のspeedにscratch_velを重ねてスクラッチ"""
        global pos, scratch_vel
        if audio_data is None or self._tt_hand_last_y is None:
            return

        dy = event.y - self._tt_hand_last_y
        self._tt_hand_last_y = event.y
        if abs(dy) < 1:
            return

        self._tt_hand_moved = True

        # ブレーキは継続したまま scratch_vel を加算（減衰速度+スクラッチの複合効果）
        factor = -dy / 15.0
        total  = float(len(audio_data))
        jump   = factor * self._TT_SAMPLES_PER_REV / (2 * math.pi)
        pos    = max(float(track_start), min(pos + jump, total - 1))
        scratch_vel = max(-20.0, min(20.0, scratch_vel + factor * 0.8))

        self._tt_angle = (self._tt_angle + factor * 0.15) % (2 * math.pi)
        self._tt_scratch_active = True
        if self._tt_cancel_id:
            self.after_cancel(self._tt_cancel_id)
        self._tt_cancel_id = self.after(200, self._tt_deactivate)
        self._draw_turntable()

    def _tt_brake_step(self):
        """摩擦による速度減衰（30msごと）: ドラッグ中も継続"""
        global speed
        if not self._tt_braking:
            return
        speed = speed * 0.9704  # 約4.5秒でほぼ停止
        if speed < 0.01:
            speed = 0.0
        self._draw_turntable()
        if speed > 0.0:
            self._tt_brake_id = self.after(30, self._tt_brake_step)

    def _tt_hand_release(self, event=None):
        """2本指クリック離し: ブレーキ解除、speed復元、scratch_vel慣性は継続"""
        global speed
        if not self._tt_braking:
            return
        self._tt_braking     = False
        self._tt_hand_last_y = None
        if self._tt_brake_id:
            self.after_cancel(self._tt_brake_id)
            self._tt_brake_id = None
        speed = self._tt_brake_speed_save   # 離したら元のspeedに戻す
        self._tt_hand_moved = False
        self._draw_turntable()

    @staticmethod
    def _fmt(secs):
        m  = int(secs) // 60
        s  = secs % 60
        return f"{m}:{s:04.1f}"

    # ── MIDI ─────────────────────────────────────────────────────
    def _open_midi(self):
        midi_in = rtmidi.MidiIn()
        ports   = midi_in.get_ports()
        idx     = None
        for i, p in enumerate(ports):
            if "nanoKONTROL2" in p or "nanokontrol2" in p.lower():
                idx = i
                break
        if idx is None and ports:
            idx = 0  # フォールバック: 最初のポート
        if idx is not None:
            midi_in.open_port(idx)
            midi_in.set_callback(self._midi_callback)
            self._midi_in = midi_in
            name = ports[idx]
            self.lbl_midi.config(text=f"MIDI: {name[:40]}", fg="#44cc88")
        else:
            self.lbl_midi.config(text="MIDI: not found", fg="#cc4444")

    def _midi_callback(self, event, data=None):
        global playing, pos, volume, speed, active_slot
        global scratch_prev
        global scratch_wide_prev, scratch_wide_last_time, scratch_wide_locked, scratch_vel
        global scratch_xwide_prev, scratch_xwide_last_time, scratch_xwide_locked, scratch_xvel
        global scratch_active, loop_rec_phase, looping, loop_start, loop_end_
        msg, _ = event
        if len(msg) < 3:
            return
        status = msg[0] & 0xF0
        cc     = msg[1]
        val    = msg[2]

        # CC メッセージのみ処理
        if status != 0xB0:
            return

        if cc == CC_VOLUME:
            volume = val / 127.0
            self.after_idle(lambda v=val: self.vol_var.set(v / 127.0 * 100))

        elif cc == CC_TEMPO:
            pct = (val - 64) / 64.0 * 50.0
            with state_lock:
                speed = 1.0 + pct / 100.0
            self.after_idle(lambda p=pct: (
                self.tempo_var.set(p),
                self.lbl_tempo.config(text=f"{p:+.1f}%")
            ))

        elif cc == CC_SCRATCH:
            if scratch_prev is not None:
                delta = val - scratch_prev
                if abs(delta) > 60:
                    delta = 0
                with state_lock:
                    if audio_data is not None:
                        pos = max(float(track_start), min(pos + delta * SAMPLE_RATE * 0.05,
                                          len(audio_data) - 1.0))
                self.after_idle(lambda d=delta: self.lbl_scratch.config(
                    text=f"SCRATCH K2: Δ{d:+d}"))
            scratch_prev = val

        elif cc == CC_SCRATCH_WIDE:
            now = time.time()
            # ロック中: 12時（64±8）に戻るまで完全無視
            if scratch_wide_locked:
                if abs(val - 64) <= 8:
                    scratch_wide_locked = False
                    scratch_wide_prev   = val
                    scratch_wide_last_time = now
                return
            # 2秒間操作なし → ロックして基準値を破棄
            if scratch_wide_prev is not None and now - scratch_wide_last_time > 1.0:
                scratch_wide_locked = True
                scratch_wide_prev   = None
                return
            if scratch_wide_prev is not None:
                delta = val - scratch_wide_prev
                if abs(delta) > 60:
                    delta = 0
                if delta != 0 and audio_data is not None:
                    jump = delta * len(audio_data) * 0.12 / 127.0
                    pos = max(float(track_start), min(pos + jump, float(len(audio_data) - 1)))
                    scratch_vel = max(-12.0, min(12.0, scratch_vel + delta * 1.5))
            scratch_wide_prev      = val
            scratch_wide_last_time = now

        elif cc == CC_SCRATCH_XWIDE:
            now = time.time()
            if scratch_xwide_locked:
                if abs(val - 64) <= 8:
                    scratch_xwide_locked    = False
                    scratch_xwide_prev      = val
                    scratch_xwide_last_time = now
                return
            if scratch_xwide_prev is not None and now - scratch_xwide_last_time > 1.0:
                scratch_xwide_locked = True
                scratch_xwide_prev   = None
                return
            if scratch_xwide_prev is not None:
                delta = val - scratch_xwide_prev
                if abs(delta) > 60:
                    delta = 0
                if delta != 0 and audio_data is not None:
                    jump = delta * len(audio_data) * 0.4 / 127.0
                    pos = max(float(track_start), min(pos + jump, float(len(audio_data) - 1)))
                    scratch_xvel = max(-30.0, min(30.0, scratch_xvel + delta * 4.0))
            scratch_xwide_prev      = val
            scratch_xwide_last_time = now

        elif cc == CC_PLAY:
            if val > 0:
                self.after_idle(self._play_stop)

        elif cc == CC_STOP:
            if val > 0:
                self.after_idle(self._stop)

        elif cc == CC_CUE:
            if val > 0:
                self.after_idle(self._toggle_reverse)

        elif cc == CC_RECORD:
            if val > 0:
                # オーディオ状態を即座に変更（after_idle不要）
                if audio_data is not None:
                    loop_start = pos
                    loop_end_  = pos + self.GLITCH_LEN
                    looping    = True
                self.after_idle(lambda: self.btn_glitch.config(fg="#ffffff", bg="#aa0000"))
            else:
                looping = False   # 即座に解除（ラグゼロ）
                self.after_idle(lambda: self.btn_glitch.config(fg="#ff44ff", bg="#2a002a"))

        elif cc == CC_LOOP:
            if val > 0:
                self.after_idle(self._rec_loop)

        elif cc in CC_SLOT:
            if val > 0:
                n = CC_SLOT.index(cc)
                self.after_idle(lambda n=n: self._select_slot(n))

    def _open_device_dialog(self):
        """出力デバイス選択ダイアログ"""
        BG, FG = "#1a1a2e", "#e0e0e0"

        # 出力チャンネルがあるデバイスだけ列挙
        devs = [(i, d) for i, d in enumerate(sd.query_devices())
                if d['max_output_channels'] > 0]

        win = tk.Toplevel(self)
        win.title("Audio Output Device")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.grab_set()

        tk.Label(win, text="Select Output Device", font=("Courier", 12, "bold"),
                 bg=BG, fg=FG).pack(pady=(12, 6), padx=16)

        frame = tk.Frame(win, bg=BG)
        frame.pack(padx=16, pady=4)

        sb = tk.Scrollbar(frame, orient="vertical")
        lb = tk.Listbox(frame, font=("Courier", 10), bg="#0d0d1a", fg=FG,
                        selectbackground="#e94560", selectforeground="#ffffff",
                        width=52, height=12, yscrollcommand=sb.set,
                        activestyle="none", bd=0)
        sb.config(command=lb.yview)
        lb.pack(side="left")
        sb.pack(side="left", fill="y")

        for i, d in devs:
            label = f"[{i:2d}] {d['name']}"
            lb.insert("end", label)
            if i == _current_device:
                lb.selection_set(lb.size() - 1)
                lb.see(lb.size() - 1)

        def _apply():
            sel = lb.curselection()
            if not sel:
                return
            idx, dev = devs[sel[0]]
            try:
                _start_stream(idx)
                win.destroy()
            except Exception as e:
                messagebox.showerror("Device Error", str(e), parent=win)

        def _reset_default():
            try:
                _start_stream(None)
                win.destroy()
            except Exception as e:
                messagebox.showerror("Device Error", str(e), parent=win)

        btn_row = tk.Frame(win, bg=BG)
        btn_row.pack(pady=10)
        for text, cmd, fg in [("Select", _apply, "#44ee88"),
                               ("Use Default", _reset_default, "#88aaff"),
                               ("Cancel", win.destroy, "#888888")]:
            tk.Label(btn_row, text=text, font=("Courier", 11, "bold"),
                     bg="#16213e", fg=fg, relief="groove", bd=2,
                     width=12, height=1, cursor="hand2").pack(
                side="left", padx=6)
        # ラベルボタンにクリックバインド
        for lbl, cmd in zip(btn_row.winfo_children(),
                            [_apply, _reset_default, win.destroy]):
            lbl.bind("<ButtonRelease-1>", lambda e, c=cmd: c())

    def on_close(self):
        global playing
        playing = False
        if self._midi_in:
            self._midi_in.close_port()
        stream.stop()
        stream.close()
        self.destroy()


# ── エントリーポイント ─────────────────────────────────────────────
if __name__ == "__main__":
    app = DJApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
