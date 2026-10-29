"""Video Event Logger — Python 3.12+, PyAV, Pillow. Video playback is silent."""
import csv
import json
import math
import queue
import threading
import time
import uuid
from pathlib import Path
import sys
vendor = Path(__file__).resolve().parent / 'vendor'
if vendor.is_dir():
    sys.path.insert(0, str(vendor))
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import av
from PIL import Image, ImageTk
from slope_measurement import SlopeTools


def timestamp(seconds):
    ms = round(max(0, seconds) * 1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f'{h:02}:{m:02}:{s:02}.{ms:03}'


class Decoder(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.commands = queue.Queue()
        self.events = queue.Queue()
        self.frames = queue.Queue(maxsize=1)
        self.container = None
        self.playing = False
        self.rate = .25
        self.position = 0
        self.buffer = None
        self.start()

    def send(self, name, value=None):
        self.commands.put((name, value))

    def frame_time(self, frame):
        return float(frame.pts * frame.time_base) - self.origin

    def seek(self, target):
        target = max(0, min(target, max(0, self.duration - 1 / self.fps)))
        self.container.seek(int((target + self.origin) / self.stream.time_base), stream=self.stream, backward=True)
        self.iterator = self.container.decode(self.stream)
        selected = None
        for frame in self.iterator:
            selected = frame
            if self.frame_time(frame) >= target - .000001:
                break
        self.buffer = None
        if selected is not None:
            self.emit(selected)
        self.anchor = time.monotonic()
        self.base = self.position

    def emit(self, frame):
        self.position = self.frame_time(frame)
        # Keep source resolution so zooming reveals the original image details.
        frame = frame.reformat(format='rgb24')
        value = (self.position, frame.to_image())
        try:
            self.frames.get_nowait()
        except queue.Empty:
            pass
        self.frames.put_nowait(value)

    def run(self):
        try:
            while True:
                try:
                    name, value = self.commands.get(timeout=.002)
                    if name == 'close':
                        return
                    if name == 'rate':
                        self.rate = value
                        self.base = self.position
                        self.anchor = time.monotonic()
                        self.events.put(('rate', value))
                    elif name == 'open':
                        self.playing = False
                        if self.container:
                            self.container.close()
                        self.container = av.open(str(value))
                        self.stream = self.container.streams.video[0]
                        self.stream.thread_type = 'AUTO'
                        self.origin = float((self.stream.start_time or 0) * self.stream.time_base)
                        self.fps = float(self.stream.average_rate or 30)
                        self.duration = float(self.stream.duration * self.stream.time_base) if self.stream.duration else float(self.container.duration or 0) / av.time_base
                        self.events.put(('metadata', dict(duration=self.duration, fps=self.fps, codec=self.stream.codec_context.name, width=self.stream.width, height=self.stream.height)))
                        self.seek(0)
                    elif self.container:
                        if name == 'seek':
                            self.playing = False
                            self.seek(value)
                            self.events.put(('paused', None))
                        elif name == 'play':
                            self.seek(value)
                            self.playing = True
                        elif name == 'pause':
                            self.playing = False
                            self.seek(value)
                except queue.Empty:
                    pass
                except Exception as exc:
                    self.playing = False
                    self.events.put(('error', str(exc)))
                if self.playing:
                    try:
                        if self.buffer is None:
                            self.buffer = next(self.iterator)
                        target = self.base + (time.monotonic() - self.anchor) * self.rate
                        if self.frame_time(self.buffer) <= target:
                            # Skip overdue frames before converting to RGB, keeping the video clock accurate.
                            chosen = self.buffer
                            self.buffer = next(self.iterator, None)
                            while self.buffer is not None and self.frame_time(self.buffer) <= target:
                                chosen = self.buffer
                                self.buffer = next(self.iterator, None)
                            self.emit(chosen)
                            if self.buffer is None:
                                self.playing = False
                                self.events.put(('ended', None))
                    except StopIteration:
                        self.playing = False
                        self.events.put(('ended', None))
                    except Exception as exc:
                        self.playing = False
                        self.events.put(('error', str(exc)))
        finally:
            if self.container:
                self.container.close()


class App(SlopeTools):
    def __init__(self, root, autosave_path=None):
        self.root = root
        self.init_slope_state()
        root.title('Video Event Logger — Python')
        root.geometry('1200x850')
        self.decoder = Decoder()
        self.path = None
        self.ready = False
        self.playing = False
        self.pos = 0
        self.duration = 0
        self.fps = 30
        self.items = []
        self.visited_videos = set()
        self.autosave_path = Path(autosave_path) if autosave_path else Path(__file__).resolve().parent / 'records_autosave.json'
        self.pending = None
        self.dragging = False
        self.applied_rate = .25
        self.overlay_state = None
        self.zoom = 1.0
        self.view_center = (.5, .5)
        self.pan_anchor = None
        top = ttk.Frame(root, padding=10)
        top.pack(fill='x')
        ttk.Button(top, text='動画を開く', command=self.open).pack(side='left')
        self.info = tk.StringVar(value='動画を選択してください。映像の観察・時刻記録用（音声なし）')
        ttk.Label(top, textvariable=self.info).pack(side='left', padx=12)
        self.canvas = tk.Canvas(root, background='#101722', highlightthickness=0, height=420)
        self.canvas.pack(fill='both', expand=True, padx=10)
        self.canvas.create_text(350, 150, text='「動画を開く」でファイルを選択', fill='white', tags='placeholder')
        self.canvas.bind('<Configure>', lambda e: self.paint())
        self.canvas.bind('<MouseWheel>', self.wheel_zoom)
        self.canvas.bind('<Button-4>', lambda e: self.wheel_zoom(e, 1))
        self.canvas.bind('<Button-5>', lambda e: self.wheel_zoom(e, -1))
        self.canvas.bind('<ButtonPress-1>', self.pan_start)
        self.canvas.bind('<B1-Motion>', self.pan_move)
        self.canvas.bind('<ButtonRelease-1>', lambda e: setattr(self, 'pan_anchor', None))
        self.canvas.bind('<Double-Button-1>', lambda e: self.pick_point(e) if self.pick_mode else self.reset_view())
        self.canvas.bind('<ButtonPress-3>', lambda e: self.pan_start(e, pan_only=True))
        self.canvas.bind('<B3-Motion>', self.pan_move)
        self.canvas.bind('<ButtonRelease-3>', lambda e: setattr(self, 'pan_anchor', None))
        self.slider = ttk.Scale(root, from_=0, to=1)
        self.slider.pack(fill='x', padx=12)
        self.slider.bind('<ButtonPress-1>', lambda e: setattr(self, 'dragging', True))
        self.slider.bind('<ButtonRelease-1>', self.scrub)
        controls = ttk.Frame(root, padding=10)
        controls.pack(fill='x')
        self.play_button = ttk.Button(controls, text='▶ 再生', command=self.toggle)
        self.play_button.pack(side='left')
        ttk.Button(controls, text='最初に戻る', command=lambda: self.seek(0)).pack(side='left', padx=3)
        for text, delta in [('−1秒', -1), ('+1秒', 1)]:
            ttk.Button(controls, text=text, command=lambda d=delta: self.seek(self.pos+d)).pack(side='left', padx=3)
        ttk.Label(controls, text='速度').pack(side='left', padx=(15, 4))
        self.rate = tk.StringVar(value='0.25')
        rate_entry = ttk.Entry(controls, textvariable=self.rate, width=7)
        rate_entry.pack(side='left')
        rate_entry.bind('<Return>', lambda e: self.set_rate())
        ttk.Button(controls, text='倍を適用', command=self.set_rate).pack(side='left', padx=3)
        presets = ttk.Frame(root, padding=(10, 0, 10, 6))
        presets.pack(fill='x')
        for rate in (.01, .1, .25, .5, 1, 2):
            ttk.Button(presets, text=f'{rate:g}倍', width=6,
                       command=lambda value=rate: self.preset_rate(value)).pack(side='left', padx=2)
        ttk.Button(presets, text='表示をリセット', command=self.reset_view).pack(side='right', padx=2)
        ttk.Button(presets, text='傾斜を測定・記録', command=self.open_slope_window).pack(side='right', padx=6)
        self.clock = tk.StringVar(value=timestamp(0))
        ttk.Label(controls, textvariable=self.clock).pack(side='right')
        recording = ttk.Frame(root, padding=10)
        recording.pack(fill='x')
        ttk.Label(recording, text='現象名・メモ').pack(side='left')
        self.name = ttk.Entry(recording, width=28)
        self.name.pack(side='left', padx=8)
        self.start_button = ttk.Button(recording, text='● 開始を記録', command=self.start_event)
        self.start_button.pack(side='left', padx=4)
        self.end_button = ttk.Button(recording, text='■ 終了を記録', command=self.end_event)
        self.end_button.pack(side='left', padx=4)
        ttk.Button(recording, text='開始を取消', command=self.cancel).pack(side='left', padx=4)
        self.status = tk.StringVar(value='時刻は動画の先頭からの経過時間です。')
        ttk.Label(root, textvariable=self.status, padding=(12, 3)).pack(fill='x')
        table_frame = ttk.Frame(root)
        table_frame.pack(fill='both', padx=10)
        self.table = ttk.Treeview(table_frame, columns=('video','name','start','end','duration'), show='headings', height=6, selectmode='browse')
        for col, title in [('video','動画名'),('name','現象名'),('start','開始'),('end','終了'),('duration','長さ')]:
            self.table.heading(col, text=title)
            self.table.column(col, width=240 if col in ('video','name') else 150, minwidth=80)
        scroll = ttk.Scrollbar(table_frame, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.table.pack(fill='both', expand=True)
        self.table.bind('<Double-1>', lambda e: self.jump())
        bottom = ttk.Frame(root, padding=10)
        bottom.pack(fill='x')
        for text, cmd in [('全動画CSVを書き出す', self.export_csv), ('全動画JSONを保存', self.export_json), ('JSONを読込', self.import_json), ('選択区間へ移動', self.jump), ('選択区間を削除', self.delete)]:
            ttk.Button(bottom, text=text, command=cmd).pack(side='left', padx=3)
        ttk.Label(root, text='Space：再生／停止　← →：1秒移動　Shift＋← →：1フレーム相当の移動　全動画の記録をまとめて自動保存', padding=(12, 4)).pack(fill='x')
        root.bind('<space>', self.key_play)
        root.bind('<Left>', lambda e: self.key_seek(e, -1))
        root.bind('<Right>', lambda e: self.key_seek(e, 1))
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.update_buttons()
        if self.autosave_path.exists():
            try:
                self.items = self.read_events(self.autosave_path)
                saved = json.loads(self.autosave_path.read_text(encoding='utf-8-sig'))
                self.slopes, self.calibrations = self.slope_data(saved)
                self.restore_calibration_default(saved)
                self.visited_videos.update(saved.get('visited_videos', []))
                self.visited_videos.update(x['video_path'] for x in self.items if x['video_path'])
                self.render()
                self.status.set(f'{len(self.items)}区間・傾斜{len(self.slopes)}件の記録を復元しました。')
            except Exception as exc:
                messagebox.showwarning('自動保存の読込', str(exc))
        root.after(15, self.poll)

    def update_buttons(self):
        self.play_button.configure(text='⏸ 一時停止' if self.playing else '▶ 再生', state='normal' if self.ready else 'disabled')
        self.start_button.configure(state='normal' if self.ready and self.pending is None else 'disabled')
        self.end_button.configure(state='normal' if self.ready and self.pending is not None else 'disabled')
        self.draw_overlay()

    def draw_overlay(self):
        state = (self.ready, self.playing, self.applied_rate, self.canvas.winfo_width(), self.zoom)
        if state == self.overlay_state:
            return
        self.overlay_state = state
        self.canvas.delete('speed_overlay')
        if not self.ready:
            return
        label = f"{'再生中' if self.playing else '一時停止'}  |  {self.applied_rate:g}倍  |  表示 {self.zoom:.0%}"
        right = self.canvas.winfo_width() - 14
        text_id = self.canvas.create_text(right, 14, anchor='ne', text=label,
                                          fill='white', font=('Yu Gothic UI', 14, 'bold'),
                                          tags='speed_overlay')
        x1, y1, x2, y2 = self.canvas.bbox(text_id)
        background = self.canvas.create_rectangle(x1-10, y1-5, x2+10, y2+5,
                                                   fill='#172331', outline='#536779',
                                                   tags='speed_overlay')
        self.canvas.tag_lower(background, text_id)

    def open(self):
        path = filedialog.askopenfilename(filetypes=[('動画', '*.mp4 *.mov *.mkv *.avi *.webm *.m4v'), ('すべて', '*.*')])
        if not path:
            return
        self.load_video(path)

    def load_video(self, path, seek_to=None):
        if self.pending is not None and not messagebox.askyesno('未確定の開始', '未確定の開始時刻を取り消して動画を開きますか？'):
            return
        self.remember_calibration(self.current_calibration())
        self.path = Path(path).resolve()
        self.ready = self.playing = False
        self.pos = 0
        self.pending = None
        self.clear_measurement()
        self.last_image = None
        self.zoom = 1.0
        self.view_center = (.5, .5)
        self.pan_anchor = None
        self.canvas.configure(cursor='')
        self.canvas.delete('all')
        self.info.set('読み込み中：' + self.path.name)
        self.render()
        self.update_buttons()
        self.decoder.send('open', path)
        if seek_to is not None:
            self.decoder.send('seek', seek_to)
        try:
            key = str(self.path)
            if key not in self.visited_videos and self.sidecar().exists():
                imported = self.read_events(self.sidecar(), key)
                known = {x['id'] for x in self.items}
                self.items.extend(x for x in imported if x['id'] not in known)
                self.render()
            self.visited_videos.add(key)
            self.autosave()
        except Exception as exc:
            messagebox.showwarning('自動保存の読込', str(exc))

    def sidecar(self):
        return self.path.with_name(self.path.name + '.events.json')

    def paint(self):
        image = getattr(self, 'last_image', None)
        if image is None:
            return
        width, height, scale = self.view_geometry()
        cx, cy = self.clamp_view(width, height, scale)
        box = (cx-width/(2*scale), cy-height/(2*scale),
               cx+width/(2*scale), cy+height/(2*scale))
        rendered = image.transform((width, height), Image.Transform.EXTENT, box,
                                   resample=Image.Resampling.BILINEAR, fillcolor='#101722')
        self.photo = ImageTk.PhotoImage(rendered)
        self.canvas.delete('all')
        self.canvas.create_image(self.canvas.winfo_width()/2, self.canvas.winfo_height()/2, image=self.photo)
        self.overlay_state = None
        self.draw_overlay()
        self.draw_measurement()

    def view_geometry(self):
        image = self.last_image
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        scale = min(width/image.width, height/image.height) * self.zoom
        return width, height, scale

    def clamp_view(self, width, height, scale):
        image = self.last_image
        if self.zoom == 1:
            self.view_center = (.5, .5)
            return image.width/2, image.height/2
        centers = []
        for ratio, size, viewport in zip(self.view_center, image.size, (width,height)):
            half = viewport/(2*scale)
            centers.append(size/2 if half >= size/2 else min(size-half, max(half, ratio*size)))
        self.view_center = (centers[0]/image.width, centers[1]/image.height)
        return centers

    def wheel_zoom(self, event, direction=None):
        if getattr(self, 'last_image', None) is None:
            return 'break'
        steps = direction if direction is not None else max(-4, min(4, event.delta/120))
        if not steps:
            return 'break'
        width, height, old_scale = self.view_geometry()
        cx, cy = self.clamp_view(width, height, old_scale)
        self.zoom = max(1.0, min(16.0, self.zoom * 1.2**steps))
        _, _, new_scale = self.view_geometry()
        self.view_center = ((cx+(event.x-width/2)*(1/old_scale-1/new_scale))/self.last_image.width,
                            (cy+(event.y-height/2)*(1/old_scale-1/new_scale))/self.last_image.height)
        self.canvas.configure(cursor='crosshair' if self.pick_mode else ('fleur' if self.zoom > 1 else ''))
        self.paint()
        return 'break'

    def pan_start(self, event, pan_only=False):
        if self.pick_mode and not pan_only:
            self.pick_point(event)
            return
        if getattr(self, 'last_image', None) is not None:
            self.pan_anchor = (event.x, event.y, self.view_center)

    def pan_move(self, event):
        if self.pan_anchor is None or self.zoom <= 1:
            return
        x, y, center = self.pan_anchor
        _, _, scale = self.view_geometry()
        self.view_center = (center[0]-(event.x-x)/(scale*self.last_image.width),
                            center[1]-(event.y-y)/(scale*self.last_image.height))
        self.paint()

    def reset_view(self):
        self.zoom = 1.0
        self.view_center = (.5, .5)
        self.pan_anchor = None
        self.canvas.configure(cursor='crosshair' if self.pick_mode else '')
        self.paint()

    def poll(self):
        try:
            while True:
                event, value = self.decoder.events.get_nowait()
                if event == 'metadata':
                    self.duration, self.fps = value['duration'], value['fps']
                    self.inherit_calibration((value['width'], value['height']))
                    self.slider.configure(to=self.duration)
                    self.info.set(f"{self.path.name} | {value['codec']} | {value['width']}×{value['height']} | {self.fps:.3f} fps")
                elif event in ('paused', 'ended'):
                    self.playing = False
                elif event == 'rate':
                    self.applied_rate = value
                elif event == 'error':
                    self.ready = self.playing = False
                    self.info.set('動画の読込・再生に失敗しました')
                    messagebox.showerror('動画エラー', value)
        except queue.Empty:
            pass
        try:
            position, image = self.decoder.frames.get_nowait()
            if not self.measure_freeze:
                self.pos, self.last_image = position, image
                self.ready = True
                self.paint()
                self.clock.set(f'{timestamp(self.pos)} / {timestamp(self.duration)}')
                if not self.dragging:
                    self.slider.set(self.pos)
        except queue.Empty:
            pass
        self.update_buttons()
        self.root.after(15, self.poll)

    def toggle(self):
        if self.ready:
            self.clear_measurement()
            self.playing = not self.playing
            target = 0 if self.pos >= self.duration - 1/self.fps else self.pos
            self.decoder.send('play' if self.playing else 'pause', target if self.playing else self.pos)
            self.update_buttons()

    def seek(self, seconds):
        if self.ready:
            self.clear_measurement()
            self.playing = False
            self.decoder.send('seek', max(0,min(self.duration,seconds)))
            self.update_buttons()

    def scrub(self, event):
        self.dragging = False
        self.seek(self.slider.get())

    def key_play(self, event):
        if not isinstance(event.widget, (ttk.Entry, tk.Entry, ttk.Button)):
            self.toggle()
            return 'break'

    def key_seek(self, event, direction):
        if not isinstance(event.widget, (ttk.Entry, tk.Entry)):
            self.seek(self.pos + direction * (1/self.fps if event.state & 1 else 1))
            return 'break'

    def preset_rate(self, value):
        self.rate.set(f'{value:g}')
        self.set_rate()

    def set_rate(self):
        try:
            rate = float(self.rate.get())
            if not math.isfinite(rate) or not .01 <= rate <= 8:
                raise ValueError()
            self.decoder.send('rate', rate)
        except ValueError:
            messagebox.showerror('再生速度', '0.01〜8の数値を入力してください。')

    def start_event(self):
        if self.ready and self.pending is None:
            self.pending = dict(id=str(uuid.uuid4()), video=self.path.name,
                                video_path=str(self.path), start=self.pos, name=self.name.get().strip())
            self.status.set('開始 ' + timestamp(self.pos) + ' — 終了ボタンで確定')
            self.update_buttons()

    def end_event(self):
        if self.pending is None:
            return
        if self.pos < self.pending['start']:
            messagebox.showerror('終了時刻', '開始時刻以降の位置で終了してください。')
            return
        self.items.append(dict(self.pending, end=self.pos))
        self.cancel()
        self.render()
        self.autosave()

    def cancel(self):
        self.pending = None
        self.status.set('開始ボタンで次の区間を記録できます。')
        self.update_buttons()

    def render(self):
        self.table.delete(*self.table.get_children())
        for i, item in enumerate(self.items):
            self.table.insert('', 'end', iid=str(i), values=(item['video'], item['name'], timestamp(item['start']), timestamp(item['end']), timestamp(item['end']-item['start'])))

    def jump(self):
        selected = self.table.selection()
        if selected:
            item = self.items[int(selected[0])]
            if item['video_path'] and self.path and Path(item['video_path']) == self.path:
                self.seek(item['start'])
            else:
                path = item['video_path']
                if not path or not Path(path).is_file():
                    messagebox.showinfo('動画の場所', f"「{item['video']}」の動画ファイルを選択してください。")
                    path = filedialog.askopenfilename(title=item['video'])
                    if not path:
                        return
                    if Path(path).name != item['video']:
                        messagebox.showerror('動画名が異なります', '記録に対応する同じ名前の動画を選択してください。')
                        return
                self.load_video(path, item['start'])

    def delete(self):
        selected = self.table.selection()
        if selected and messagebox.askyesno('区間を削除', '選択した区間を削除しますか？'):
            del self.items[int(selected[0])]
            self.render()
            self.autosave()

    def payload(self):
        return dict(version=3, time_basis='seconds from each video start',
                    visited_videos=sorted(self.visited_videos), events=self.items,
                    slopes=self.slopes, calibrations=self.calibrations,
                    last_calibration=self.last_calibration)

    def write_json(self, path):
        path = Path(path)
        temp = path.with_name(path.name + '.tmp')
        temp.write_text(json.dumps(self.payload(), ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)

    def autosave(self):
        try:
            self.write_json(self.autosave_path)
            videos={x.get('video_path') or x['video'] for x in self.items+self.slopes}
            self.status.set(f"{len(videos)}動画・{len(self.items)}区間・傾斜{len(self.slopes)}件を自動保存しました。")
        except OSError as exc:
            messagebox.showwarning('自動保存できません', f'JSONを保存から別の場所へ保存してください。\n{exc}')

    def export_json(self):
        path = filedialog.asksaveasfilename(defaultextension='.json', initialfile='all_video_events.json')
        if path:
            try:
                self.write_json(path)
            except OSError as exc:
                messagebox.showerror('保存エラー', str(exc))

    @staticmethod
    def read_events(path, default_video_path=''):
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        items = data['events']
        if not isinstance(items, list):
            raise ValueError('区間データが配列ではありません。')
        for x in items:
            if not isinstance(x, dict) or any(type(x.get(k)) not in (int,float) or not math.isfinite(x[k]) for k in ('start','end')) or not 0 <= x['start'] <= x['end']:
                raise ValueError('開始・終了時刻が正しくありません。')
        result = []
        legacy_video = str(data.get('video','')).split('|')[0]
        for i,x in enumerate(items):
            video_path = str(x.get('video_path', default_video_path))
            video = str(x.get('video') or legacy_video or (Path(video_path).name if video_path else ''))
            if not video:
                raise ValueError('動画名のない記録です。対象動画を開いて旧版のJSONを読み込んでください。')
            identity = str(x.get('id') or uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([video_path or video,i,x],ensure_ascii=False,sort_keys=True)))
            result.append(dict(id=identity,video=video,video_path=video_path,start=x['start'],end=x['end'],name=str(x.get('name',''))))
        return result

    def import_json(self):
        path = filedialog.askopenfilename(filetypes=[('JSON','*.json')])
        if path:
            try:
                data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
                fallback = str(self.path) if self.path and (not data.get('video') or str(data['video']).split('|')[0] == self.path.name) else ''
                items = self.read_events(path, fallback)
                slopes, calibrations = self.slope_data(data)
                if not messagebox.askyesno('記録を読込', '全動画の記録一覧を、選択したJSONの内容に置き換えますか？'):
                    return
                self.items = items
                self.slopes, self.calibrations = slopes, calibrations
                self.restore_calibration_default(data)
                self.visited_videos = set(data.get('visited_videos', []))
                self.visited_videos.update(x['video_path'] for x in items if x['video_path'])
                self.visited_videos.update(x.get('video_path','') for x in slopes if x.get('video_path'))
                self.clear_measurement()
                self.render_slopes()
                self.cancel()
                self.render()
                self.autosave()
            except Exception as exc:
                messagebox.showerror('読込エラー', str(exc))

    def export_csv(self):
        path = filedialog.asksaveasfilename(defaultextension='.csv', initialfile='all_video_events.csv')
        if path:
            try:
                with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                    writer = csv.writer(f)
                    writer.writerow(['No','Video','Video_path','Event','Start_s','End_s','Duration_s','Start_time','End_time'])
                    for i,x in enumerate(self.items,1):
                        def cell(text):
                            return "'"+text if text.lstrip().startswith(('=','+','-','@')) else text
                        writer.writerow([i,cell(x['video']),cell(x['video_path']),cell(x['name']),f"{x['start']:.6f}",f"{x['end']:.6f}",f"{x['end']-x['start']:.6f}",timestamp(x['start']),timestamp(x['end'])])
            except OSError as exc:
                messagebox.showerror('保存エラー', str(exc))

    def close(self):
        if self.pending is not None and not messagebox.askyesno('未確定の開始', '未確定の開始時刻を取り消して終了しますか？'):
            return
        self.decoder.send('close')
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    App(root)
    root.mainloop()
