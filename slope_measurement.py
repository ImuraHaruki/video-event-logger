"""Image-plane slope measurement tools for Video Event Logger."""
import csv
import json
import math
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog


def calculate_slope(points, calibration):
    a, b = points
    if b[0] < a[0]:
        a, b = b, a
    c, d = calibration['points']
    pixels = math.dist(c, d)
    length = calibration['length']
    if not math.isfinite(length) or length <= 0 or pixels < 1e-6 or math.dist(a,b) < 1e-6:
        raise ValueError('異なる2点と、0より大きい既知の長さが必要です。')
    scale = length / pixels
    dx, dy = (b[0]-a[0])*scale, (a[1]-b[1])*scale
    return dict(points=[list(a),list(b)], angle_deg=math.degrees(math.atan2(dy,dx)),
                slope_percent=100*dy/dx if abs(dx)>1e-12 else None,
                horizontal_length=dx, vertical_length=dy, length=math.hypot(dx,dy),
                unit=calibration['unit'], calibration=json.loads(json.dumps(calibration)))


class SlopeTools:
    def init_slope_state(self):
        self.slopes = []
        self.calibrations = {}
        self.last_calibration = None
        self.pick_mode = None
        self.pick_points = []
        self.measure_points = []
        self.measure_freeze = False
        self.measure_time = None
        self.slope_window = None

    @staticmethod
    def slope_data(data):
        slopes = data.get('slopes', [])
        calibrations = data.get('calibrations', {})
        if not isinstance(slopes,list) or not isinstance(calibrations,dict):
            raise ValueError('傾斜データの形式が正しくありません。')
        def check_calibration(c):
            pts=c['points']
            if len(pts)!=2 or any(len(p)!=2 or any(type(n) not in (int,float) or not math.isfinite(n) for n in p) for p in pts):
                raise ValueError('基準点が正しくありません。')
            if type(c['length']) not in (int,float) or not math.isfinite(c['length']) or c['length']<=0 or math.dist(*pts)<1e-6:
                raise ValueError('既知の長さが正しくありません。')
            if not isinstance(c['unit'],str): raise ValueError('単位が正しくありません。')
        for c in calibrations.values(): check_calibration(c)
        normalized=[]
        for s in slopes:
            if not isinstance(s,dict) or not s.get('video') or type(s.get('time')) not in (int,float) or not math.isfinite(s['time']) or s['time']<0:
                raise ValueError('傾斜記録が正しくありません。')
            check_calibration(s['calibration'])
            pts=s['points']
            if len(pts)!=2 or any(len(p)!=2 or any(type(n) not in (int,float) or not math.isfinite(n) for n in p) for p in pts):
                raise ValueError('測定点が正しくありません。')
            row=dict(s)
            row.update(calculate_slope(pts,s['calibration']))
            normalized.append(row)
        return normalized, dict(calibrations)

    def open_slope_window(self):
        if self.slope_window is not None and self.slope_window.winfo_exists():
            self.slope_window.lift(); return
        w=self.slope_window=tk.Toplevel(self.root)
        w.title('傾斜を測定・記録'); w.geometry('1050x520')
        ttk.Label(w,text='①既知の長さを入力 → 基準2点を指定　②傾斜の2点を指定　③傾斜を記録',padding=10).pack(fill='x')
        row=ttk.Frame(w,padding=10); row.pack(fill='x')
        ttk.Label(row,text='既知の長さ').pack(side='left')
        current = self.current_calibration() or self.last_calibration or {}
        self.known_length=tk.StringVar(value=f"{current.get('length',1):g}")
        ttk.Entry(row,textvariable=self.known_length,width=10).pack(side='left',padx=5)
        self.length_unit=tk.StringVar(value=current.get('unit','cm'))
        ttk.Entry(row,textvariable=self.length_unit,width=7).pack(side='left')
        for text,cmd in [('基準2点を指定',lambda:self.begin_pick('calibration')),('傾斜2点を指定',lambda:self.begin_pick('slope')),('傾斜を記録',self.record_slope),('点指定を取消',self.cancel_pick)]:
            ttk.Button(row,text=text,command=cmd).pack(side='left',padx=4)
        self.slope_status=tk.StringVar(value='基準の長さは画像内の同じ平面で指定してください。')
        ttk.Label(w,textvariable=self.slope_status,padding=10,wraplength=1000).pack(fill='x')
        ttk.Label(w,text='画像の横方向が水平／右上がり＋・右下がり−。画像上の角度と長さです（遠近・レンズ歪みの補正なし）。',padding=10).pack(fill='x')
        ttk.Label(w,text='動画は指定開始時に一時停止。動画画面で左クリックして2点を指定します。ホイールでズーム、右ドラッグで移動。',padding=10).pack(fill='x')
        frame=ttk.Frame(w); frame.pack(fill='both',expand=True,padx=10)
        self.slope_table=ttk.Treeview(frame,columns=('video','name','time','angle','grade','length'),show='headings',height=8)
        for key,title in [('video','動画名'),('name','メモ'),('time','動画内秒'),('angle','角度 °'),('grade','勾配 %'),('length','長さ')]:
            self.slope_table.heading(key,text=title); self.slope_table.column(key,width=150,minwidth=70)
        scroll=ttk.Scrollbar(frame,orient='vertical',command=self.slope_table.yview)
        self.slope_table.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y'); self.slope_table.pack(fill='both',expand=True)
        bottom=ttk.Frame(w,padding=10); bottom.pack(fill='x')
        ttk.Button(bottom,text='全動画の傾斜CSVを書き出す',command=self.export_slopes_csv).pack(side='left',padx=4)
        ttk.Button(bottom,text='全記録JSONを保存',command=self.export_json).pack(side='left',padx=4)
        ttk.Button(bottom,text='選択した傾斜を削除',command=self.delete_slope).pack(side='left',padx=4)
        self.render_slopes()
        w.protocol('WM_DELETE_WINDOW',self.close_slope_window)

    def close_slope_window(self):
        self.cancel_pick()
        self.slope_window.destroy(); self.slope_window=None

    def slope_message(self,text):
        if hasattr(self,'slope_status'): self.slope_status.set(text)
        self.status.set(text)

    def current_calibration(self):
        return self.calibrations.get(str(self.path)) if self.path else None

    def remember_calibration(self, calibration):
        if calibration:
            self.last_calibration = json.loads(json.dumps(calibration))

    def restore_calibration_default(self, data):
        last = data.get('last_calibration')
        if last is None and self.calibrations:
            last = list(self.calibrations.values())[-1]
        if last:
            self.slope_data({'calibrations': {'last': last}})
        self.last_calibration = json.loads(json.dumps(last)) if last else None

    def inherit_calibration(self, image_size):
        if not self.path:
            return
        key = str(self.path)
        current = self.current_calibration()
        if current:
            self.remember_calibration(current)
        elif self.last_calibration:
            calibration = json.loads(json.dumps(self.last_calibration))
            previous = calibration.get('image_size', image_size)
            sx, sy = image_size[0]/previous[0], image_size[1]/previous[1]
            if not math.isclose(sx, sy, rel_tol=.005):
                self.slope_message('動画の縦横比が異なるため、基準2点を指定し直してください。')
                return
            calibration['points'] = [[x*sx, y*sy] for x,y in calibration['points']]
            calibration['image_size'] = list(image_size)
            self.calibrations[key] = calibration
            self.remember_calibration(calibration)
            self.autosave()
            self.slope_message(f"基準2点を引き継ぎました：{calibration['length']:g} {calibration['unit']}。")
        current = self.current_calibration()
        if current and self.slope_window is not None and self.slope_window.winfo_exists():
            self.known_length.set(f"{current['length']:g}")
            self.length_unit.set(current['unit'])

    def begin_pick(self, mode):
        if not self.ready or not self.path:
            messagebox.showinfo('動画を選択','先に測定する動画を開いてください。'); return
        if mode=='calibration':
            try:
                length=float(self.known_length.get())
                if not math.isfinite(length) or length<=0 or not self.length_unit.get().strip(): raise ValueError()
                self.pick_calibration_length=length
                self.pick_calibration_unit=self.length_unit.get().strip()
            except ValueError:
                messagebox.showerror('既知の長さ','0より大きい長さと単位を入力してください。'); return
        elif not self.current_calibration():
            messagebox.showinfo('基準を指定','先に、この動画の基準2点と既知の長さを指定してください。'); return
        self.playing=False
        self.decoder.send('pause',self.pos)
        self.measure_freeze=True
        self.measure_time=self.pos
        self.pick_mode=mode; self.pick_points=[]; self.measure_points=[]
        self.canvas.configure(cursor='crosshair')
        self.slope_message('動画画面で'+('既知の長さの基準' if mode=='calibration' else '傾斜を測る線')+'の2点をクリックしてください。')
        self.paint(); self.update_buttons()

    def pick_point(self,event):
        w,h,scale=self.view_geometry(); cx,cy=self.clamp_view(w,h,scale)
        point=[cx+(event.x-w/2)/scale,cy+(event.y-h/2)/scale]
        if not (0<=point[0]<self.last_image.width and 0<=point[1]<self.last_image.height): return
        if self.pick_points and math.dist(point,self.pick_points[0])<1:
            self.slope_message('1点目から離れた点を指定してください。'); return
        self.pick_points.append(point)
        if len(self.pick_points)==2:
            if self.pick_mode=='calibration':
                self.calibrations[str(self.path)]=dict(points=self.pick_points.copy(),length=self.pick_calibration_length,
                    unit=self.pick_calibration_unit, time=self.measure_time,
                    image_size=list(self.last_image.size))
                self.remember_calibration(self.current_calibration())
                self.autosave()
                self.slope_message(f'基準を設定：{self.pick_calibration_length:g} {self.pick_calibration_unit}。次に「傾斜2点を指定」を押してください。')
            else:
                self.measure_points=self.pick_points.copy()
                result=calculate_slope(self.measure_points,self.current_calibration())
                self.slope_message(f"角度 {result['angle_deg']:.3f}°、長さ {result['length']:.4g} {result['unit']}。「傾斜を記録」で保存します。")
            self.pick_mode=None; self.pick_points=[]
            self.canvas.configure(cursor='fleur' if self.zoom>1 else '')
        self.paint()

    def cancel_pick(self):
        self.pick_mode=None; self.pick_points=[]; self.measure_points=[]
        self.canvas.configure(cursor='fleur' if self.zoom>1 else '')
        self.paint()

    def clear_measurement(self):
        self.pick_mode=None; self.pick_points=[]; self.measure_points=[]
        self.measure_freeze=False; self.measure_time=None

    def draw_measurement(self):
        if getattr(self,'last_image',None) is None: return
        w,h,scale=self.view_geometry(); cx,cy=self.clamp_view(w,h,scale)
        cal=self.current_calibration()
        groups=[]
        if cal: groups.append((cal['points'],'#5de3ff','C'))
        if self.measure_points: groups.append((self.measure_points,'#ffdf63','P'))
        if self.pick_mode: groups.append((self.pick_points,'#ffdf63','P' if self.pick_mode=='slope' else 'C'))
        for points,color,prefix in groups:
            coords=[(w/2+(x-cx)*scale,h/2+(y-cy)*scale) for x,y in points]
            if len(coords)==2: self.canvas.create_line(*coords[0],*coords[1],fill=color,width=2)
            for i,(x,y) in enumerate(coords,1):
                self.canvas.create_oval(x-4,y-4,x+4,y+4,outline=color,width=2)
                self.canvas.create_text(x+8,y-10,text=f'{prefix}{i}',fill=color,anchor='w',font=('Arial',12,'bold'))

    def record_slope(self):
        if len(self.measure_points)!=2 or not self.current_calibration() or self.measure_time is None:
            messagebox.showinfo('測定点を指定','基準2点と傾斜の2点を指定してください。'); return
        result=calculate_slope(self.measure_points,self.current_calibration())
        result.update(id=str(uuid.uuid4()),video=self.path.name,video_path=str(self.path),
                      time=self.measure_time,name=self.name.get().strip(),image_size=list(self.last_image.size))
        self.slopes.append(result); self.measure_points=[]
        self.render_slopes(); self.autosave(); self.paint()
        self.slope_message(f"傾斜 {result['angle_deg']:.3f}° を記録しました（全{len(self.slopes)}件）。")

    def render_slopes(self):
        if self.slope_window is None or not self.slope_window.winfo_exists(): return
        self.slope_table.delete(*self.slope_table.get_children())
        for i,s in enumerate(self.slopes):
            self.slope_table.insert('','end',iid=str(i),values=(s['video'],s['name'],f"{s['time']:.6f}",f"{s['angle_deg']:.3f}",
                '垂直' if s['slope_percent'] is None else f"{s['slope_percent']:.3f}",f"{s['length']:.5g} {s['unit']}"))

    def delete_slope(self):
        selected=self.slope_table.selection()
        if selected and messagebox.askyesno('傾斜を削除','選択した傾斜記録を削除しますか？'):
            del self.slopes[int(selected[0])]; self.render_slopes(); self.autosave()

    def export_slopes_csv(self):
        path=filedialog.asksaveasfilename(defaultextension='.csv',initialfile='all_video_slopes.csv')
        if not path: return
        def cell(text):
            text=str(text)
            return "'"+text if text.lstrip().startswith(('=','+','-','@')) else text
        try:
            with open(path,'w',newline='',encoding='utf-8-sig') as f:
                writer=csv.writer(f)
                writer.writerow(['No','Video','Video_path','Name','Time_s','Angle_deg','Slope_percent','Horizontal_length','Vertical_length','Length','Unit',
                    'P1_x_px','P1_y_px','P2_x_px','P2_y_px','Calibration_length','C1_x_px','C1_y_px','C2_x_px','C2_y_px'])
                for i,s in enumerate(self.slopes,1):
                    c=s['calibration']
                    writer.writerow([i,cell(s['video']),cell(s.get('video_path','')),cell(s.get('name','')),s['time'],s['angle_deg'],
                        '' if s['slope_percent'] is None else s['slope_percent'],s['horizontal_length'],s['vertical_length'],s['length'],cell(s['unit']),
                        *s['points'][0],*s['points'][1],c['length'],*c['points'][0],*c['points'][1]])
        except OSError as exc: messagebox.showerror('保存エラー',str(exc))
