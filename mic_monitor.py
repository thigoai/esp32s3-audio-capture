#!/usr/bin/env python3
"""
Monitor de microfone I2S (ESP32-S3) com filtros e ganho ajustaveis.

- Le as amostras (uma por linha, inteiro de 16 bits) pela serial
- Mostra forma de onda e espectro em tempo real
- Medidor de pico com aviso de CLIPPING
- Sliders HP/LP/Ganho enviam "HP=<hz>", "LP=<hz>" e "GAIN=<linear>" para a placa
- Botao Gravar salva um WAV (16 kHz, mono, 16 bits) com os ajustes no nome
  e um .txt com a configuracao usada (para repetir na inferencia)

Uso:
    pip install pyserial numpy matplotlib
    python mic_monitor.py                      (abre uma janela para escolher a porta)
    python mic_monitor.py --port /dev/ttyACM1  (pula a janela)

Feche o monitor serial do PlatformIO antes: so um programa usa a porta.
"""
import argparse
import math
import threading
import time
import wave
from datetime import datetime

import matplotlib.pyplot as plt
import numpy as np
import serial
from serial.tools import list_ports
from matplotlib.animation import FuncAnimation
from matplotlib.widgets import Button, Slider
import json
from pathlib import Path

CONFIG_FILE = Path(__file__).with_name("mic_monitor_config.json")

SAMPLE_RATE = 16000
WAVE_SECONDS = 0.5           # janela da forma de onda e do medidor de pico
FFT_SIZE = 4096              # amostras usadas no espectro
RING_SIZE = SAMPLE_RATE * 4  # historico em memoria
CLIP_LEVEL = 32700           # a partir daqui consideramos clipping


class SerialReader(threading.Thread):
    """Le a serial em segundo plano e separa amostras de mensagens de texto."""

    def __init__(self, ser):
        super().__init__(daemon=True)
        self.ser = ser
        self.lock = threading.Lock()
        self.ring = np.zeros(RING_SIZE, dtype=np.int16)
        self.total = 0
        self.recording = False
        self.rec_chunks = []
        self.running = True

    def run(self):
        pending = b""
        while self.running:
            try:
                chunk = self.ser.read(max(1, self.ser.in_waiting))
            except (serial.SerialException, OSError) as e:
                print(f"[erro serial] {e}")
                break
            if not chunk:
                continue
            pending += chunk
            lines = pending.split(b"\n")
            pending = lines.pop()       # ultimo pedaco pode estar incompleto

            samples = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    samples.append(int(line))
                except ValueError:
                    print("[placa]", line.decode(errors="replace"))

            if samples:
                arr = np.clip(samples, -32768, 32767).astype(np.int16)
                with self.lock:
                    n = len(arr)
                    if n >= RING_SIZE:
                        self.ring[:] = arr[-RING_SIZE:]
                    else:
                        self.ring = np.roll(self.ring, -n)
                        self.ring[-n:] = arr
                    self.total += n
                    if self.recording:
                        self.rec_chunks.append(arr)

    def latest(self, n):
        with self.lock:
            return self.ring[-n:].copy()

    def start_recording(self):
        with self.lock:
            self.rec_chunks = []
            self.recording = True

    def stop_recording(self, prefix="gravacao"):
        with self.lock:
            self.recording = False
            chunks, self.rec_chunks = self.rec_chunks, []
        if not chunks:
            return None
        data = np.concatenate(chunks)
        name = f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.wav"
        with wave.open(name, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(data.tobytes())
        mag = np.abs(data.astype(np.int32))     # int32 evita overflow de abs(-32768)
        peak = int(mag.max())
        n_clip = int(np.count_nonzero(mag >= CLIP_LEVEL))
        return name, len(data) / SAMPLE_RATE, peak, n_clip


def salvar_config(wav_name, params_ini, params_fim, dur, peak, n_clip, baud):
    """Grava um .txt ao lado do WAV com a configuracao usada na gravacao.

    Devolve o nome do .txt. params_* = (hp_hz, lp_hz, ganho_db).
    """
    hp, lp, db = params_ini
    ganho = 10 ** (db / 20)
    dbfs = 20 * math.log10(max(peak, 1) / 32768)
    mudou = params_fim != params_ini

    linhas = [
        f"arquivo_wav = {wav_name}",
        f"data = {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"duracao_s = {dur:.2f}",
        "",
        "# Configuracao do processamento (a usar tambem na inferencia)",
        f"sample_rate = {SAMPLE_RATE}",
        f"hp_hz = {hp:.1f}",
        f"lp_hz = {lp:.1f}",
        f"ganho_db = {db:.1f}",
        f"ganho_linear = {ganho:.3f}",
        f"baud = {baud}",
        "",
        "# Resultado da gravacao",
        f"pico = {peak} ({peak / 32768 * 100:.0f}% da escala, {dbfs:.1f} dBFS)",
        f"amostras_clipadas = {n_clip}",
    ]
    if mudou:
        hp2, lp2, db2 = params_fim
        linhas += [
            "",
            "# ATENCAO: os sliders mudaram durante a gravacao.",
            "# Os valores acima sao os do INICIO; ao parar estavam em:",
            f"# hp_hz = {hp2:.1f}, lp_hz = {lp2:.1f}, ganho_db = {db2:.1f}",
        ]
    linhas += [
        "",
        "// Constantes para colar no firmware (src/main.cpp)",
        f"const int SAMPLE_RATE = {SAMPLE_RATE};",
        f"float GAIN = {ganho:.3f}f;",
        f"float HP_CUTOFF = {hp:.1f}f;",
        f"float LP_CUTOFF = {lp:.1f}f;",
        "",
        "# Processamento fixo do firmware (precisa ser identico na inferencia):",
        "#  raw >> 8 (24 bits) -> passa-alto 1a ordem -> passa-baixo 1a ordem",
        "#  -> / 256 -> x GAIN -> limite em [-32768, 32767] (PCM 16 bits)",
        "#  hp_alpha = exp(-2*pi*HP/fs);  lp_alpha = 1 - exp(-2*pi*LP/fs)",
    ]

    txt_name = wav_name[: -len(".wav")] + ".txt"
    with open(txt_name, "w", encoding="utf-8") as f:
        f.write("\n".join(linhas) + "\n")
    return txt_name

def carregar_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def guardar_config(hp, lp, db):
    try:
        CONFIG_FILE.write_text(json.dumps({"hp": hp, "lp": lp, "gain_db": db}))
    except OSError as e:
        print(f"[aviso] nao salvei a config: {e}")

def listar_portas():
    """Retorna [(dispositivo, descricao, vid), ...] das portas seriais."""
    portas = []
    for p in sorted(list_ports.comports(), key=lambda p: p.device):
        desc = p.description if p.description and p.description != "n/a" else ""
        portas.append((p.device, desc, p.vid))
    return portas


def abrir_serial(porta, baud):
    # Abre sem derrubar DTR/RTS, para a placa nao resetar ao conectar
    ser = serial.Serial()
    ser.port = porta
    ser.baudrate = baud
    ser.timeout = 0.05
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def escolher_porta_terminal(baud):
    """Plano B, usado se o tkinter nao estiver instalado."""
    portas = listar_portas()
    if not portas:
        print("Nenhuma porta serial encontrada.")
        return None
    for i, (dev, desc, _) in enumerate(portas):
        print(f"[{i}] {dev}  {desc}")
    try:
        return portas[int(input("Numero da porta: "))][0], baud
    except (ValueError, IndexError, EOFError):
        return None


def escolher_porta(baud, erro=None):
    """Janela para escolher porta e baud. Retorna (porta, baud) ou None."""
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        if erro:
            print(erro)
        return escolher_porta_terminal(baud)

    resultado = {}
    dispositivos = []               # lista paralela as opcoes do combobox

    root = tk.Tk()
    root.title("Selecionar porta serial")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=14)
    frm.grid()

    ttk.Label(frm, text="Porta:").grid(row=0, column=0, sticky="w")
    combo = ttk.Combobox(frm, state="readonly", width=50)
    combo.grid(row=0, column=1, columnspan=2, padx=6, pady=4)

    ttk.Label(frm, text="Baud:").grid(row=1, column=0, sticky="w")
    var_baud = tk.StringVar(value=str(baud))
    ttk.Entry(frm, textvariable=var_baud, width=12).grid(
        row=1, column=1, sticky="w", padx=6, pady=4)

    var_msg = tk.StringVar()
    ttk.Label(frm, textvariable=var_msg, foreground="#b00020",
              wraplength=400, justify="left").grid(
        row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
    ttk.Label(frm, text="Feche o monitor serial do PlatformIO antes de conectar.",
              foreground="#666666").grid(
        row=3, column=0, columnspan=3, sticky="w", pady=(2, 8))

    def atualizar(mostrar_erro=False):
        anterior = None
        i = combo.current()
        if 0 <= i < len(dispositivos):
            anterior = dispositivos[i]
        portas = listar_portas()
        dispositivos[:] = [p[0] for p in portas]
        combo["values"] = [f"{d} - {desc}" if desc else d for d, desc, _ in portas]
        if portas:
            if anterior in dispositivos:
                idx = dispositivos.index(anterior)
            else:   # prefere uma porta USB da Espressif (ESP32-S3), se houver
                idx = next((k for k, p in enumerate(portas) if p[2] == 0x303A), 0)
            combo.current(idx)
            var_msg.set(erro if (mostrar_erro and erro) else "")
        else:
            combo.set("")
            var_msg.set("Nenhuma porta encontrada. Conecte a placa e clique em Atualizar.")

    def conectar(_event=None):
        i = combo.current()
        if i < 0:
            messagebox.showwarning("Porta", "Nenhuma porta selecionada.")
            return
        try:
            b = int(var_baud.get())
        except ValueError:
            messagebox.showwarning("Baud", "Baud invalido.")
            return
        resultado["porta"] = dispositivos[i]
        resultado["baud"] = b
        root.destroy()

    botoes = ttk.Frame(frm)
    botoes.grid(row=4, column=0, columnspan=3, sticky="e")
    ttk.Button(botoes, text="Atualizar", command=atualizar).grid(row=0, column=0, padx=4)
    ttk.Button(botoes, text="Conectar", command=conectar).grid(row=0, column=1, padx=4)
    root.bind("<Return>", conectar)

    atualizar(mostrar_erro=True)
    root.attributes("-topmost", True)               # traz a janela para frente
    root.after(300, lambda: root.attributes("-topmost", False))
    root.mainloop()

    if "porta" not in resultado:
        return None
    return resultado["porta"], resultado["baud"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None,
                    help="ex.: /dev/ttyACM1 ou COM5 (se omitido, abre uma janela)")
    ap.add_argument("--baud", type=int, default=2000000)
    ap.add_argument("--hp", type=float, default=None, help="passa-alto (Hz)")
    ap.add_argument("--lp", type=float, default=None, help="passa-baixo (Hz)")
    ap.add_argument("--gain-db", type=float, default=None, help="ganho em dB")
    args = ap.parse_args()

    cfg = carregar_config()
    if args.hp is None:
        args.hp = cfg.get("hp", 100.0)
    if args.lp is None:
        args.lp = cfg.get("lp", 7000.0)
    if args.gain_db is None:
        args.gain_db = cfg.get("gain_db", 24.0)

    # Escolha da porta: --port pula a janela; se a abertura falhar, ela reaparece
    porta, baud = args.port, args.baud
    ser = None
    erro = None
    while ser is None:
        if porta is None:
            escolha = escolher_porta(baud, erro)
            if escolha is None:
                print("Nenhuma porta escolhida.")
                return
            porta, baud = escolha
        try:
            ser = abrir_serial(porta, baud)
        except (serial.SerialException, OSError) as e:
            erro = f"Nao consegui abrir {porta}: {e}"
            if "ermission" in str(e):
                erro += "  (no Linux: sudo usermod -aG dialout $USER e relogue)"
            print("[erro]", erro)
            porta = None
    print(f"[conectado] {porta} @ {baud}")

    reader = SerialReader(ser)
    reader.start()

    def send(cmd):
        ser.write((cmd + "\n").encode())

    # ---------- interface ----------
    fig, (ax_w, ax_s) = plt.subplots(2, 1, figsize=(10, 7.5))
    fig.subplots_adjust(bottom=0.30, hspace=0.35)

    n_wave = int(SAMPLE_RATE * WAVE_SECONDS)
    t = np.linspace(-WAVE_SECONDS, 0, n_wave)
    (line_w,) = ax_w.plot(t, np.zeros(n_wave), lw=0.8)
    ax_w.set_ylim(-32768, 32767)
    ax_w.set_xlim(-WAVE_SECONDS, 0)
    ax_w.set_xlabel("tempo (s)")
    ax_w.set_ylabel("amplitude")
    ax_w.set_title("Forma de onda")
    ax_w.grid(alpha=0.3)
    ax_w.axhline(CLIP_LEVEL, color="tab:red", lw=0.6, alpha=0.5)
    ax_w.axhline(-CLIP_LEVEL, color="tab:red", lw=0.6, alpha=0.5)
    peak_txt = ax_w.text(0.01, 0.90, "", transform=ax_w.transAxes,
                         fontsize=10, family="monospace",
                         bbox=dict(facecolor="white", alpha=0.8, lw=0))

    freqs = np.fft.rfftfreq(FFT_SIZE, 1 / SAMPLE_RATE)
    window = np.hanning(FFT_SIZE)
    (line_s,) = ax_s.plot(freqs, np.full_like(freqs, -120.0), lw=0.8)
    ax_s.set_xlim(0, SAMPLE_RATE / 2)
    ax_s.set_ylim(-20, 100)
    ax_s.set_xlabel("frequencia (Hz)")
    ax_s.set_ylabel("dB")
    ax_s.set_title("Espectro")
    ax_s.grid(alpha=0.3)
    vline_hp = ax_s.axvline(args.hp, color="tab:red", ls="--", lw=1)
    vline_lp = ax_s.axvline(args.lp, color="tab:green", ls="--", lw=1)

    # Sliders mais estreitos para o valor aparecer inteiro ao lado
    ax_hp = fig.add_axes([0.12, 0.19, 0.58, 0.03])
    ax_lp = fig.add_axes([0.12, 0.13, 0.58, 0.03])
    ax_g = fig.add_axes([0.12, 0.07, 0.58, 0.03])
    sl_hp = Slider(ax_hp, "HP (Hz)", 20, 1000, valinit=args.hp, valstep=5)
    sl_lp = Slider(ax_lp, "LP (Hz)", 500, 7900, valinit=args.lp, valstep=50)
    sl_g = Slider(ax_g, "Ganho (dB)", 0, 60, valinit=args.gain_db, valstep=1)

    ax_btn = fig.add_axes([0.82, 0.07, 0.13, 0.10])
    btn = Button(ax_btn, "Gravar")
    status = fig.text(0.82, 0.19, "", fontsize=9)

    def current_params():
        hp, lp, db = sl_hp.val, sl_lp.val, sl_g.val
        if lp <= hp + 50:               # evita LP colado ou abaixo do HP
            lp = hp + 50
        return hp, lp, db

    last_sent = {"t": 0.0}

    def send_params():
        hp, lp, db = current_params()
        send(f"HP={hp:.1f}")
        send(f"LP={lp:.1f}")
        send(f"GAIN={10 ** (db / 20):.3f}")

    def on_slider(_=None, force=False):
        hp, lp, _db = current_params()
        vline_hp.set_xdata([hp, hp])
        vline_lp.set_xdata([lp, lp])
        now = time.time()
        if force or now - last_sent["t"] > 0.1:
            send_params()
            last_sent["t"] = now

    sl_hp.on_changed(on_slider)
    sl_lp.on_changed(on_slider)
    sl_g.on_changed(on_slider)
    # garante que o valor final seja enviado ao soltar o mouse
    fig.canvas.mpl_connect(
        "button_release_event", lambda e: on_slider(force=True)
    )

    rec = {"params": None}

    def label(params):
        hp, lp, db = params
        return f"HP{hp:.0f}_LP{lp:.0f}_G{db:.0f}dB"

    def toggle_record(_):
        if not reader.recording:
            rec["params"] = current_params()
            reader.start_recording()
            btn.label.set_text("Parar")
            status.set_text("gravando...")
        else:
            prefix = "gravacao_" + label(rec["params"])
            changed = current_params() != rec["params"]
            if changed:
                prefix += "_AJUSTE-MUDOU"
            result = reader.stop_recording(prefix)
            btn.label.set_text("Gravar")
            if result:
                name, dur, peak, n_clip = result
                txt = salvar_config(name, rec["params"], current_params(),
                                    dur, peak, n_clip, baud)
                warn = "\nATENCAO: ajustes\nmudaram na gravacao" if changed else ""
                clip = f"\n{n_clip} amostras clipadas" if n_clip else ""
                status.set_text(f"salvo ({dur:.1f} s) + .txt{warn}{clip}")
                print(f"[gravacao] {name} ({dur:.1f} s)")
                print(f"[config]   {txt}")
            else:
                status.set_text("nada gravado")

    btn.on_clicked(toggle_record)

    def update(_):
        w = reader.latest(n_wave).astype(np.float32)
        line_w.set_ydata(w)

        peak = float(np.max(np.abs(w))) if w.size else 0.0
        pct = peak / 32768 * 100
        dbfs = 20 * np.log10(max(peak, 1.0) / 32768)
        clip = peak >= CLIP_LEVEL
        peak_txt.set_text(
            f"pico {pct:3.0f}%  ({dbfs:5.1f} dBFS)" + ("  CLIPPING" if clip else "")
        )
        peak_txt.set_color("tab:red" if clip else "black")

        s = reader.latest(FFT_SIZE).astype(np.float32)
        spec = np.abs(np.fft.rfft(s * window)) / (FFT_SIZE / 2)
        line_s.set_ydata(20 * np.log10(spec + 1e-6) + 20)
        return line_w, line_s, peak_txt

    anim = FuncAnimation(fig, update, interval=50, blit=False,
                         cache_frame_data=False)

    time.sleep(0.3)
    send_params()                       # sincroniza placa com os sliders
    try:
        plt.show()
    finally:
        guardar_config(*current_params())
        reader.running = False
        if reader.recording:
            reader.stop_recording("gravacao_incompleta")
        ser.close()
    return anim


if __name__ == "__main__":
    main()