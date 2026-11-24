using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Linq;
using System.Windows.Automation;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Markup;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Threading;
using Microsoft.Win32;

namespace Meter {
    public class Widget : Window {
        readonly string origin, zone, positionFile;
        readonly JavaScriptSerializer json = new JavaScriptSerializer();
        readonly StackPanel body = new StackPanel();
        readonly TextBlock stamp = new TextBlock();
        readonly Dictionary<string,Button> tabs = new Dictionary<string,Button>();
        readonly DispatcherTimer timer = new DispatcherTimer();
        readonly DispatcherTimer followTimer = new DispatcherTimer();
        readonly Button pin, refreshBtn;
        readonly System.Windows.Shapes.Path refreshGlyph;
        readonly ScrollViewer scroller = new ScrollViewer();
        string period = "today";
        int serial;
        string displayedPeriod, lastRaw, loadingPeriod;
        DateTime lastActivate = DateTime.MinValue;

        [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr FindWindow(string cls,string title);
        [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
        [STAThread] public static void Main(string[] args) {
            int port;
            string root = AppDomain.CurrentDomain.BaseDirectory;
            if (args.Length == 0 || (args.Length==1 && args[0]=="--ledger")) {
                string launchArgs="-B -X utf8 ledger_app.py --windowless"+(args.Length==0?" --widget":"");
                var start = new ProcessStartInfo(Path.Combine(root,"runtime","python.exe"),launchArgs);
                start.WorkingDirectory=root; start.UseShellExecute=false; start.CreateNoWindow=true;
                try { Process.Start(start); } catch(Exception e) { MessageBox.Show(e.Message,"刻度"); }
                return;
            }
            if (!int.TryParse(args[0],out port) || port<1 || port>65535) return;
            bool first;
            using (var mutex = new Mutex(true,"Local\\MeterWidget-"+port,out first)) {
                string title="刻度 · 桌面小组件 "+port;
                if (!first) { SetForegroundWindow(FindWindow(null,title)); return; }
                new Application().Run(new Widget(port,args.Length>1 ? args[1] : "",title,
                    args.Length>2 ? args[2] : Path.Combine(root,"private","widget-window.json")));
            }
        }
        static string DefaultZone() {
            var zones=new Dictionary<string,string>{{"Tokyo Standard Time","Asia/Tokyo"},{"China Standard Time","Asia/Shanghai"},
                {"Taipei Standard Time","Asia/Taipei"},{"Pacific Standard Time","America/Los_Angeles"},{"Eastern Standard Time","America/New_York"},
                {"GMT Standard Time","Europe/London"},{"W. Europe Standard Time","Europe/Berlin"},{"Korea Standard Time","Asia/Seoul"}};
            string name; return zones.TryGetValue(TimeZoneInfo.Local.Id,out name) ? name : "UTC";
        }
        static SolidColorBrush Brush(string hex) {return new SolidColorBrush((Color)ColorConverter.ConvertFromString(hex));}
        static SolidColorBrush Mix(Color a,Color b,double t) {return new SolidColorBrush(Color.FromRgb((byte)(a.R+(b.R-a.R)*t),(byte)(a.G+(b.G-a.G)*t),(byte)(a.B+(b.B-a.B)*t)));}
        static double Linear(byte v) { double x=v/255.0; return x<=.04045?x/12.92:Math.Pow((x+.055)/1.055,2.4); }
        static double Luminance(Color c) { return .2126*Linear(c.R)+.7152*Linear(c.G)+.0722*Linear(c.B); }
        static Color Ink(Color fg,Color bg) {
            double a=Luminance(fg),b=Luminance(bg);
            return (Math.Max(a,b)+.05)/(Math.Min(a,b)+.05)>=4.5?fg:b>.179?Colors.Black:Colors.White;
        }
        static LinearGradientBrush Finish(Color top,Color bottom,byte alpha) {
            top.A=alpha;bottom.A=alpha;
            var brush=new LinearGradientBrush(top,bottom,90);brush.Freeze();return brush;
        }
        static Dictionary<string,object> Obj(object v) {return v as Dictionary<string,object>;}
        TextBlock Label(string text,double size,string color) {
            var t=new TextBlock { Text=text,FontSize=size,VerticalAlignment=VerticalAlignment.Center, TextTrimming=TextTrimming.CharacterEllipsis };
            t.SetResourceReference(TextBlock.ForegroundProperty,color); return t;
        }
        Button Action(string label,string tip,Action click) {
            var b=new Button {BorderThickness=new Thickness(0),BorderBrush=Brushes.Transparent,Content=label, ToolTip=tip, Margin=new Thickness(1,0,1,0), Padding=new Thickness(8,4,8,4),Cursor=Cursors.Hand};
            b.SetResourceReference(Control.ForegroundProperty,"fg");
            b.Click+=(s,e)=>click();return b;
        }
        static FrameworkElement Glyph(string geometry, int size, string color, bool filled=false) {
            var path=new System.Windows.Shapes.Path {Data=Geometry.Parse(geometry), Width=size, Height=size,
                Stretch=Stretch.Uniform, StrokeThickness=1.4, StrokeLineJoin=PenLineJoin.Round,
                StrokeStartLineCap=PenLineCap.Round, StrokeEndLineCap=PenLineCap.Round};
            path.SetResourceReference(filled?System.Windows.Shapes.Shape.FillProperty:System.Windows.Shapes.Shape.StrokeProperty,color);
            return path;
        }
        Widget(int port,string requestedZone,string title,string preferencesFile) {
            origin="http://127.0.0.1:"+port;zone=String.IsNullOrEmpty(requestedZone)?DefaultZone():requestedZone;
            positionFile=preferencesFile;
            Title=title; Width=356;Height=298;WindowStyle=WindowStyle.None;ResizeMode=ResizeMode.NoResize;
            UseLayoutRounding=true;
            AllowsTransparency=true;Background=Brushes.Transparent;
            Topmost=true;FontFamily=new FontFamily("Microsoft YaHei UI");
            Left=SystemParameters.WorkArea.Right-Width-24;Top=SystemParameters.WorkArea.Top+36;
            try {
                var saved=json.Deserialize<Dictionary<string,double>>(File.ReadAllText(positionFile));
                var rect=new Rect(saved["x"],saved["y"],Width,Height);
                if(System.Windows.Forms.Screen.AllScreens.Length==1) {
                    if(SystemParameters.WorkArea.Contains(rect)) {Left=rect.X;Top=rect.Y;}
                } else if (new Rect(SystemParameters.VirtualScreenLeft,SystemParameters.VirtualScreenTop,SystemParameters.VirtualScreenWidth,SystemParameters.VirtualScreenHeight).Contains(rect)) {Left=rect.X;Top=rect.Y;}
                Topmost=saved["pin"]==1;
            } catch(Exception) { }
            Resources["surface"]=Brush("#171719");Resources["fg"]=Brush("#F2EEE8");Resources["sub"]=Brush("#ABA7A4");Resources["accent"]=Brush("#E79B69");Resources["line"]=Brush("#3B3836");
            Resources.Add(typeof(Button),XamlReader.Parse(@"<Style xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' TargetType='Button'>
              <Setter Property='Background' Value='Transparent'/><Setter Property='Template'><Setter.Value>
              <ControlTemplate TargetType='Button'><Border Name='box' Background='{TemplateBinding Background}' BorderBrush='{TemplateBinding BorderBrush}' BorderThickness='{TemplateBinding BorderThickness}' Padding='{TemplateBinding Padding}' CornerRadius='5'>
              <ContentPresenter HorizontalAlignment='Center' VerticalAlignment='Center'/></Border><ControlTemplate.Triggers>
              <Trigger Property='IsMouseOver' Value='True'><Setter TargetName='box' Property='Background' Value='{DynamicResource hover}'/></Trigger>
              </ControlTemplate.Triggers></ControlTemplate></Setter.Value></Setter></Style>"));
            scroller.Resources.Add(typeof(System.Windows.Controls.Primitives.ScrollBar),XamlReader.Parse(@"<Style xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' xmlns:x='http://schemas.microsoft.com/winfx/2006/xaml' TargetType='ScrollBar'>
              <Setter Property='Width' Value='6'/><Setter Property='Template'><Setter.Value><ControlTemplate TargetType='ScrollBar'>
              <Track x:Name='PART_Track' IsDirectionReversed='True' Orientation='Vertical'><Track.Thumb><Thumb>
              <Thumb.Template><ControlTemplate TargetType='Thumb'><Grid Background='Transparent'><Border Width='3' HorizontalAlignment='Right' CornerRadius='1.5' Background='{DynamicResource line}'/></Grid></ControlTemplate></Thumb.Template>
              </Thumb></Track.Thumb></Track></ControlTemplate></Setter.Value></Setter></Style>"));
            var frame=new Border {CornerRadius=new CornerRadius(14),BorderThickness=new Thickness(1)};
            frame.SetResourceReference(Border.BackgroundProperty,"surface");
            frame.SetResourceReference(Border.BorderBrushProperty,"edge");Content=frame;
            var bevel=new Border {CornerRadius=new CornerRadius(13),BorderThickness=new Thickness(1),Padding=new Thickness(15,10,15,7)};
            bevel.SetResourceReference(Border.BorderBrushProperty,"bevel");frame.Child=bevel;
            var layout=new DockPanel();bevel.Child=layout;
            var head=new DockPanel {LastChildFill=true};DockPanel.SetDock(head,Dock.Top);layout.Children.Add(head);
            var close=Action("×","关闭小组件",Close);close.Content=Glyph("M2,2 L12,12 M12,2 L2,12",10,"sub");AutomationProperties.SetName(close,"关闭小组件");DockPanel.SetDock(close,Dock.Right);head.Children.Add(close);
            pin=Action("","切换窗口置顶",()=>{Topmost=!Topmost;PinLabel();});PinLabel();DockPanel.SetDock(pin,Dock.Right);head.Children.Add(pin);
            refreshGlyph=(System.Windows.Shapes.Path)Glyph("M12.2,7.5 A5.2,5.2 0 1,1 10.7,3.8 M10.7,1.3 L10.7,3.8 L8.2,3.8",12,"sub");
            refreshBtn=Action("","立即同步最新数据",()=>ForceRefresh());refreshBtn.Content=refreshGlyph;AutomationProperties.SetName(refreshBtn,"刷新数据");DockPanel.SetDock(refreshBtn,Dock.Right);head.Children.Add(refreshBtn);
            var brand=new StackPanel {Orientation=Orientation.Horizontal, Background=Brushes.Transparent,Cursor=Cursors.SizeAll};
            brand.Children.Add(Glyph("M1,9 L4,8 7,20 4,21 Z M6,4 L9,3 13,19 10,20 Z M12,1 L15,0 20,17 17,18 Z",19,"accent",true));
            var name=Label("刻度",15,"fg");name.FontWeight=FontWeights.SemiBold;name.Margin=new Thickness(7,0,8,0);brand.Children.Add(name);
            brand.Children.Add(Label("用量账本",11,"sub"));brand.MouseLeftButtonDown+=(s,e)=>{if(e.LeftButton==MouseButtonState.Pressed) DragMove();};head.Children.Add(brand);
            var foot=new DockPanel();DockPanel.SetDock(foot,Dock.Bottom);layout.Children.Add(foot);
            var open=Action("打开账本 ↗","打开当前视图",()=>Process.Start(origin+(period=="quota"?"/?tab=book&booktab=quota":"/?period="+period)+"&tz="+Uri.EscapeDataString(zone)));
            DockPanel.SetDock(open,Dock.Right);foot.Children.Add(open);stamp.FontSize=10;stamp.VerticalAlignment=VerticalAlignment.Center;stamp.SetResourceReference(TextBlock.ForegroundProperty,"sub");foot.Children.Add(stamp);
            var periods=new System.Windows.Controls.Primitives.UniformGrid {Rows=1};
            var rail=new Border {CornerRadius=new CornerRadius(7),Padding=new Thickness(3),Margin=new Thickness(0,9,0,6),Child=periods};
            rail.SetResourceReference(Border.BackgroundProperty,"rail");DockPanel.SetDock(rail,Dock.Top);layout.Children.Add(rail);
            foreach(var p in new[]{new[]{"today","今天"},new[]{"week","近 7 天"},new[]{"quota","订阅额度"}}) {
                string id=p[0];var b=Action(p[1],p[1],()=>{if(period==id)return;period=id;Update();});b.FontSize=11;tabs[id]=b;periods.Children.Add(b);
            }
            var rule=new Border{Height=1,Margin=new Thickness(0,4,0,3)};rule.SetResourceReference(Border.BackgroundProperty,"line");
            DockPanel.SetDock(rule,Dock.Bottom);layout.Children.Add(rule);
            open.FontSize=10;open.SetResourceReference(Control.ForegroundProperty,"sub");
            scroller.VerticalScrollBarVisibility=ScrollBarVisibility.Auto;scroller.HorizontalScrollBarVisibility=ScrollBarVisibility.Disabled;scroller.Content=body;layout.Children.Add(scroller);
            Loaded+=(s,e)=>Update();timer.Interval=TimeSpan.FromSeconds(8);timer.Tick+=(s,e)=>Update();timer.Start();
            followTimer.Interval=TimeSpan.FromMilliseconds(1200);followTimer.Tick+=(s,e)=>Update();
            Activated+=(s,e)=>{if((DateTime.UtcNow-lastActivate).TotalSeconds>=1.5){lastActivate=DateTime.UtcNow;Update();}};
            Closed+=(s,e)=>{timer.Stop();followTimer.Stop();try{Directory.CreateDirectory(Path.GetDirectoryName(positionFile));File.WriteAllText(positionFile,json.Serialize(new {x=Left,y=Top,pin=Topmost?1:0}));}catch(IOException){}};
        }
        void PinLabel(){
            pin.Content=Glyph("M5,1 L12,1 M6,1 L6,6 3,9 14,9 11,6 11,1 M8.5,9 L8.5,14",13,Topmost?"accent":"sub");
            pin.ToolTip=Topmost?"取消置顶":"置顶";AutomationProperties.SetName(pin,Topmost?"已置顶":"置顶");
        }
        void SelectTab(){
            foreach(var pair in tabs){
                bool selected=pair.Key==period;
                pair.Value.Background=selected?(Brush)Resources["selected"]:Brushes.Transparent;
                pair.Value.BorderThickness=new Thickness(1);
                pair.Value.BorderBrush=selected?(Brush)Resources["bevel"]:Brushes.Transparent;
                pair.Value.SetResourceReference(Control.ForegroundProperty,selected?"fg":"sub");
                pair.Value.FontWeight=selected?FontWeights.SemiBold:FontWeights.Normal;
            }
        }
        static string WindowSpan(object minutes) {
            if (minutes == null) return "额度窗口";
            int n=Convert.ToInt32(minutes);
            if (n>0 && n%1440==0) return (n/1440)+" 天";
            if (n>0 && n%60==0) return (n/60)+" 小时";
            return n+" 分钟";
        }
        void Theme(Dictionary<string,object> prefs) {
            string mode=(string)prefs["mode"];
            if(mode=="system") mode=Convert.ToInt32(Registry.GetValue(@"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize","AppsUseLightTheme",1))==0?"dark":"light";
            var colors=(IList)prefs[mode];var bg=Brush((string)colors[1]);var fg=Brush((string)colors[2]);
            bool high=SystemParameters.HighContrast;
            if(high)bg=new SolidColorBrush(SystemColors.WindowColor);
            fg=new SolidColorBrush(high?SystemColors.WindowTextColor:Ink(fg.Color,bg.Color));
            Color accent=high?SystemColors.HighlightColor:Brush((string)colors[0]).Color;
            Resources["surface"]=Finish(Mix(bg.Color,accent,.034).Color,bg.Color,(byte)(high?255:mode=="dark"?239:246));
            double baseLight=Luminance(bg.Color),markLight=Luminance(accent);
            bool quiet=(Math.Max(baseLight,markLight)+.05)/(Math.Min(baseLight,markLight)+.05)<2;
            Resources["fg"]=fg;Resources["accent"]=quiet?Mix(accent,fg.Color,.75):new SolidColorBrush(accent);
            Resources["sub"]=high || (baseLight>.08 && baseLight<.38)?fg:Mix(bg.Color,fg.Color,.75);Resources["line"]=Mix(bg.Color,fg.Color,.20);
            Resources["edge"]=Finish(Mix(bg.Color,fg.Color,.34).Color,Mix(bg.Color,fg.Color,.19).Color,255);
            Resources["bevel"]=Finish(Mix(bg.Color,Colors.White,mode=="dark"?.14:.80).Color,Mix(bg.Color,accent,.08).Color,215);
            string font=(string)prefs["font"];FontFamily=new FontFamily(font=="系统默认"?"Microsoft YaHei UI":font=="等宽"?"Consolas, Microsoft YaHei UI":font);
            Resources["rail"]=Mix(bg.Color,fg.Color,.055);Resources["hover"]=Mix(bg.Color,fg.Color,.12);
            Resources["selected"]=Finish(Mix(bg.Color,accent,.24).Color,Mix(bg.Color,accent,.12).Color,255);SelectTab();
        }
        static Grid Columns(params double[] weights) {
            var grid=new Grid();
            foreach(double weight in weights) grid.ColumnDefinitions.Add(new ColumnDefinition{Width=new GridLength(weight,GridUnitType.Star)});
            return grid;
        }
        static void Place(Grid grid,UIElement child,int col){
            Grid.SetColumn(child,col);grid.Children.Add(child);
        }
        static Grid Meter(double share) {
            share=Math.Max(0,Math.Min(1,share));
            var bar=Columns(share,1-share);
            bar.Height=4;bar.Margin=new Thickness(0,4,0,1);
            var track=new Border {CornerRadius=new CornerRadius(2)};
            track.SetResourceReference(Border.BackgroundProperty,"line");
            Grid.SetColumnSpan(track,2);bar.Children.Add(track);
            var fill=new Border{CornerRadius=new CornerRadius(2)};
            fill.SetResourceReference(Border.BackgroundProperty,"accent");Place(bar,fill,0);
            return bar;
        }
        static string WindowName(Dictionary<string,object> row,string group) {
            object label;
            if(!row.TryGetValue("label",out label))return WindowSpan(row.ContainsKey("minutes")?row["minutes"]:null);
            string text=Convert.ToString(label);
            if(group.Length>0 && text.StartsWith(group+" ",StringComparison.OrdinalIgnoreCase))text=text.Substring(group.Length+1);
            switch(text.ToLowerInvariant()){
                case "weekly limit remaining": case "weekly limit": return "每周";
                case "five hour limit remaining": case "five hour limit": return "5 小时";
                default:return text;
            }
        }
        FrameworkElement QuotaCell(Dictionary<string,object> row,string group,object captured,bool captionVisible=true) {
            var cell=new StackPanel{Margin=new Thickness(8,0,0,0)};
            string caption=WindowName(row,group);
            if(captionVisible)cell.Children.Add(Label(caption,10,"sub"));
            bool expired=row["reset"]!=null && Convert.ToDouble(row["reset"])<=DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            bool measured=!expired && !row.ContainsKey("state") && row["remaining"]!=null;
            double percent=measured?Convert.ToDouble(row["remaining"]):0;
            string value=expired?"周期已结束":row.ContainsKey("state")?Convert.ToString(row["state"]):measured?percent.ToString("0.##")+"%":"未提供";
            var reading=Label(value,measured?17:12,measured?"fg":"sub");
            if(measured)reading.FontFamily=new FontFamily("Consolas");cell.Children.Add(reading);
            if(measured)cell.Children.Add(Meter(percent/100));
            cell.ToolTip=group+" "+(row.ContainsKey("label")?row["label"]:caption)+"  ·  "+value+"\n快照 "+captured;
            return cell;
        }
        void DrawQuota(IList sources,bool syncing){
            DateTimeOffset latest=DateTimeOffset.MinValue;
            foreach(object s in sources){
                var o=Obj(s);object atObj;DateTimeOffset dto;
                if(o!=null && o.TryGetValue("at",out atObj) && atObj!=null && DateTimeOffset.TryParse(Convert.ToString(atObj),CultureInfo.InvariantCulture,DateTimeStyles.AssumeUniversal,out dto) && dto>latest) latest=dto;
            }
            stamp.Text=syncing?"剩余额度 · 更新中…":latest>DateTimeOffset.MinValue?"剩余额度 · "+latest.ToLocalTime().ToString("HH:mm"):"剩余额度 · 上次快照";
            foreach(object source in sources){
                var src=Obj(source);
                var groups=((IList)src["windows"]).Cast<object>().Select(Obj)
                    .GroupBy(row=>row.ContainsKey("group")?Convert.ToString(row["group"]):"").ToList();
                var captions=groups.SelectMany(g=>g.Select(row=>WindowName(row,g.Key))).Distinct().ToList();
                bool sharedHead=groups.Count>0 && groups.All(g=>g.Key.Length>0 && g.Select(row=>WindowName(row,g.Key)).Distinct().Count()==g.Count()) && captions.Count<=2;
                if(body.Children.Count>0){
                    var rule=new Border {Height=1,Margin=new Thickness(0,6,0,4)};
                    rule.SetResourceReference(Border.BackgroundProperty,"line");body.Children.Add(rule);
                }
                if(groups.Any(g=>g.Key.Length>0)){
                    var section=Label(Convert.ToString(src["name"]),13,"fg");section.FontWeight=FontWeights.SemiBold;
                    section.Margin=new Thickness(0,2,0,1);
                    if(sharedHead){
                        var heading=Columns(1.45,1,1);heading.Margin=new Thickness(0,1,4,3);
                        Place(heading,section,0);
                        for(int i=0;i<captions.Count;i++){
                            var title=Label(captions[i],10,"sub");title.Margin=new Thickness(8,0,0,0);title.ToolTip=captions[i];Place(heading,title,i+1);
                        }
                        body.Children.Add(heading);
                    }else body.Children.Add(section);
                }
                foreach(var group in groups){
                    var rows=group.ToList();
                    for(int i=0;i<rows.Count;i+=2){
                        var grid=Columns(1.45,1,1);grid.Margin=new Thickness(0,3,4,4);
                        if(i==0){
                            string title=group.Key.Length==0?Convert.ToString(src["name"]):group.Key;
                            if(title.Equals("Gemini Models",StringComparison.OrdinalIgnoreCase))title="Gemini";
                            if(title.Equals("Claude and GPT models",StringComparison.OrdinalIgnoreCase))title="Claude 与 GPT";
                            var name=Label(title,group.Key.Length==0?13:12,"fg");
                            name.FontWeight=group.Key.Length==0?FontWeights.SemiBold:FontWeights.Normal;
                            name.TextWrapping=TextWrapping.Wrap;name.ToolTip=group.Key.Length==0?title:group.Key;
                            Place(grid,name,0);
                        }
                        for(int j=i;j<Math.Min(rows.Count,i+2);j++){
                            int col=sharedHead?1+captions.IndexOf(WindowName(rows[j],group.Key)):rows.Count==1?2:1+j-i;
                            Place(grid,QuotaCell(rows[j],group.Key,src["at"],!sharedHead),col);
                        }
                        body.Children.Add(grid);
                    }
                }
            }
            if(body.Children.Count==0)Empty("尚无订阅额度快照","打开账本，在剩余额度页查询一次。");
        }
        void Empty(string title,string hint){
            var name=Label(title,17,"fg");name.Margin=new Thickness(0,22,0,4);body.Children.Add(name);body.Children.Add(Label(hint,12,"sub"));
        }
        void DrawUsage(Dictionary<string,object> data,bool syncing) {
            var t=Obj(data["totals"]);stamp.Text=syncing?zone+" · 同步中…":zone;
            if(Convert.ToInt64(t["n"])==0){Empty("当前时段还没有用量记录","在账本的数据接入页更新来源。");return;}
            var summary=new DockPanel{Margin=new Thickness(0,8,0,0)};
            var records=Label(string.Format("{0:N0} 条记录",t["n"]),10,"sub");DockPanel.SetDock(records,Dock.Right);summary.Children.Add(records);
            var titleRow=new StackPanel{Orientation=Orientation.Horizontal};
            titleRow.Children.Add(Label("Token 总量",11,"sub"));
            object valObj;
            if(data.TryGetValue("value",out valObj) && valObj!=null) {
                double usd=Convert.ToDouble(valObj,CultureInfo.InvariantCulture);
                string fmt=usd>0 && usd<0.01?"0.0000":"#,##0.00";
                var usdLabel=Label("≈ $"+usd.ToString(fmt,CultureInfo.InvariantCulture),11,"accent");
                usdLabel.FontFamily=new FontFamily("Consolas");
                usdLabel.Margin=new Thickness(8,0,0,0);
                usdLabel.ToolTip="按公开 API 标价估算，仅供参考";
                titleRow.Children.Add(usdLabel);
            }
            summary.Children.Add(titleRow);body.Children.Add(summary);
            var total=Label(t["total"]==null?"未提供":Convert.ToDecimal(t["total"]).ToString("N0"),36,"fg");
            total.FontFamily=new FontFamily("Consolas");total.ToolTip=total.Text;
            total.Margin=new Thickness(0,2,0,8);Typography.SetNumeralAlignment(total,FontNumeralAlignment.Tabular);body.Children.Add(total);
            var stats=Columns(1.4,1);var panel=new Border{CornerRadius=new CornerRadius(8),Padding=new Thickness(11,8,11,8),Child=stats};
            panel.SetResourceReference(Border.BackgroundProperty,"rail");body.Children.Add(panel);
            var model=new StackPanel {Margin=new Thickness(0,0,12,0)};Place(stats,model,0);model.Children.Add(Label("用量最高的模型",10,"sub"));
            var top=Label(Convert.ToString(data["top"]??"未提供"),13,"fg");top.FontWeight=FontWeights.SemiBold;top.ToolTip=top.Text;top.Margin=new Thickness(0,4,0,0);
            top.TextWrapping=TextWrapping.Wrap;top.MaxHeight=38;model.Children.Add(top);
            var cache=new StackPanel();Place(stats,cache,1);cache.Children.Add(Label("缓存占输入",10,"sub"));
            bool known=t["cache_read"]!=null && t["input"]!=null && Convert.ToDouble(t["input"])>0 &&
                Convert.ToInt64(t["input_n"])==Convert.ToInt64(t["n"]) && Convert.ToInt64(t["cache_read_n"])==Convert.ToInt64(t["n"]);
            double share=known?Convert.ToDouble(t["cache_read"])/Convert.ToDouble(t["input"]):0;
            var fraction=Label(known?share.ToString("0.#%"):"—",15,"fg");fraction.FontFamily=new FontFamily("Consolas");cache.Children.Add(fraction);
            if(known)cache.Children.Add(Meter(share));else fraction.ToolTip="缺少可核对的完整缓存及输入数据";
            var counts=Columns(1.4,1);counts.Margin=new Thickness(11,10,11,0);
            int col=0;
            foreach(string field in new[]{"input","output"}){
                var stat=new StackPanel();Place(counts,stat,col++);
                stat.Children.Add(Label(field=="input"?"输入 Token":"输出 Token",10,"sub"));
                bool partial=Convert.ToInt64(t[field+"_n"])<Convert.ToInt64(t["n"]);
                var count=Label(t[field]==null?"—":Convert.ToDecimal(t[field]).ToString("N0"),13,"fg");
                count.FontFamily=new FontFamily("Consolas");count.ToolTip=count.Text+(partial?" · 仅已知记录":"");
                if(t[field]!=null && partial)count.Text+=" *";
                stat.Children.Add(count);
            }
            body.Children.Add(counts);
        }
        async void ForceRefresh() {
            lastRaw=null;
            refreshGlyph.SetResourceReference(System.Windows.Shapes.Shape.StrokeProperty,"accent");
            stamp.Text=period=="quota"?"剩余额度 · 更新中…":zone+" · 同步中…";
            if(!followTimer.IsEnabled)followTimer.Start();
            try {
                using(var client=new WebClient())
                using(var timeout=new CancellationTokenSource(TimeSpan.FromSeconds(20)))
                using(timeout.Token.Register(client.CancelAsync)){
                    client.Proxy=null;client.Encoding=System.Text.Encoding.UTF8;
                    client.Headers["X-Ledger-Local"]="1";
                    await client.UploadStringTaskAsync(origin+"/api/widget/refresh?period="+period,"POST","");
                }
            } catch(Exception err) {
                if(!(err is WebException || err is OperationCanceledException)) throw;
                followTimer.Stop();
                stamp.Text="刷新请求失败 · 可再试一次";
                refreshGlyph.SetResourceReference(System.Windows.Shapes.Shape.StrokeProperty,"sub");
                return;
            }
            Update();
        }
        async void Update() {
            if(loadingPeriod==period)return;
            loadingPeriod=period;
            int ticket=++serial;
            try {
                string raw;
                using(var client=new WebClient())
                using(var timeout=new CancellationTokenSource(TimeSpan.FromSeconds(30)))
                using(timeout.Token.Register(client.CancelAsync)){
                    client.Proxy=null;client.Encoding=System.Text.Encoding.UTF8;
                    raw=await client.DownloadStringTaskAsync(origin+"/api/widget?period="+period+"&tz="+Uri.EscapeDataString(zone));
                }
                if(ticket!=serial)return;
                if(raw==lastRaw && displayedPeriod==period)return;
                lastRaw=raw;
                var data=json.Deserialize<Dictionary<string,object>>(raw);
                object syncObj;
                bool syncing=data.TryGetValue("syncing",out syncObj) && syncObj!=null && Convert.ToBoolean(syncObj);
                refreshGlyph.SetResourceReference(System.Windows.Shapes.Shape.StrokeProperty,syncing?"accent":"sub");
                if(syncing){if(!followTimer.IsEnabled)followTimer.Start();}
                else if(followTimer.IsEnabled)followTimer.Stop();
                Theme(Obj(data["appearance"]));body.Children.Clear();
                if(period=="quota") DrawQuota((IList)data["quotas"],syncing); else DrawUsage(data,syncing);
                if(displayedPeriod!=period) {
                    scroller.ScrollToTop();
                    if(SystemParameters.ClientAreaAnimation && !SystemParameters.HighContrast) {
                        var fade=new DoubleAnimation(.28,1,TimeSpan.FromMilliseconds(140)) {FillBehavior=FillBehavior.Stop};
                        body.BeginAnimation(UIElement.OpacityProperty,fade);
                    }
                    displayedPeriod=period;
                }
            } catch(Exception) {if(ticket==serial){lastRaw=null;period=displayedPeriod??period;SelectTab();stamp.Text="账本连接中断 · 稍后自动重试";}}
            finally {if(ticket==serial)loadingPeriod=null;}
        }
    }
}
