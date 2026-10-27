// PetHub Light —— WebView2 Composition 视觉托管壳
// 透明原理：DirectComposition 视觉树 + WS_EX_LAYERED 分层窗口，支持逐像素 alpha，
// 这是 WebView2 官方唯一支持的桌面透明方案（HWND 托管的 TransparencyKey 均不可用）。
using Microsoft.Web.WebView2.Core;
using System;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using Windows.UI.Composition.Desktop;
using WinRT;

static class Program
{
    // ---------------- Win32 ----------------
    const int GWL_EXSTYLE = -20;
    const int WS_EX_LAYERED = 0x00080000;
    const int WS_EX_TRANSPARENT = 0x00000020;
    const int WS_EX_TOOLWINDOW = 0x00000080;
    const int WS_EX_NOACTIVATE = 0x08000000;
    const int WS_EX_NOREDIRECTIONBITMAP = 0x00200000;   // 关键：窗口不自绘背景，DComp 内容独享逐像素 alpha
    const int LWA_ALPHA = 2;

    [DllImport("user32.dll")] static extern int GetWindowLong(IntPtr h, int i);
    [DllImport("user32.dll")] static extern int SetWindowLong(IntPtr h, int i, int v);
    [DllImport("user32.dll")] static extern bool SetLayeredWindowAttributes(IntPtr h, uint key, byte alpha, int flags);
    [DllImport("user32.dll")] static extern bool GetCursorPos(out POINT p);
    [DllImport("user32.dll")] static extern bool SetCursorPos(int X, int Y);
    struct POINT { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);

    // z 序：桌宠可以待在"桌面之上、其他窗口之下"，点开别的应用就看不见它
    static readonly IntPtr HWND_BOTTOM = new IntPtr(1);
    static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
    const uint SWP_NOSIZE = 0x0001, SWP_NOMOVE = 0x0002, SWP_NOACTIVATE = 0x0010, SWP_NOOWNERZORDER = 0x0200;
    [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);

    // ---- 全局低层鼠标钩子：窗口"永远穿透"，只在命中宠物/HUD 时吞掉并转发给页面 ----
    const int WM_MOUSEMOVE = 0x0200, WM_LBUTTONDOWN = 0x0201, WM_LBUTTONUP = 0x0202,
              WM_RBUTTONDOWN = 0x0204, WM_RBUTTONUP = 0x0205, WM_MOUSEWHEEL = 0x020A;

    // 全局热键（任何时候都能救命：WebView2 万一重建了吃点击的宿主窗口）
    const int WM_HOTKEY = 0x0312;
    const uint MOD_ALT = 0x0001, MOD_CONTROL = 0x0002;
    const int HK_QUIT = 1, HK_TOGGLE = 2, HK_LAYER = 3;
    [DllImport("user32.dll")] static extern bool RegisterHotKey(IntPtr h, int id, uint mods, uint vk);
    [DllImport("user32.dll")] static extern bool UnregisterHotKey(IntPtr h, int id);

    const int WH_MOUSE_LL = 14;
    delegate IntPtr HookProc(int nCode, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] static extern IntPtr SetWindowsHookEx(int idHook, HookProc lpfn, IntPtr hMod, uint dwThreadId);
    [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr hhk);
    [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr hhk, int nCode, IntPtr wParam, IntPtr lParam);
    [StructLayout(LayoutKind.Sequential)]
    struct MSLLHOOKSTRUCT { public POINT pt; public uint mouseData, flags, time; public IntPtr dwExtraInfo; }
    delegate bool EnumWindowsProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr h, EnumWindowsProc f, IntPtr l);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowsProc f, IntPtr l);

    // 进程树快照（找出本进程拉起的 msedgewebview2.exe）
    const uint TH32CS_SNAPPROCESS = 0x00000002;
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct PROCESSENTRY32W
    {
        public uint dwSize, cntUsage, th32ProcessID;
        public IntPtr th32DefaultHeapID;
        public uint th32ModuleID, cntThreads, th32ParentProcessID;
        public int pcPriClassBase;
        public uint dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string szExeFile;
    }
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint pid);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern bool Process32FirstW(IntPtr h, ref PROCESSENTRY32W e);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern bool Process32NextW(IntPtr h, ref PROCESSENTRY32W e);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int GetClassName(IntPtr h, System.Text.StringBuilder sb, int max);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);

    // 未打包应用创建 WinRT Compositor 前必须先建 DispatcherQueue（coremessaging.dll）
    struct DispatcherQueueOptions { public int dwSize, threadType, apartmentType; }
    [DllImport("coremessaging.dll", EntryPoint = "CreateDispatcherQueueController", PreserveSig = false)]
    static extern void CreateDispatcherQueueController(DispatcherQueueOptions options, out IntPtr controller);

    // Windows.UI.Composition 互操作（.NET 10 禁用 IInspectable 封送，手动 vtable 调用）
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    delegate int QIFn(IntPtr self, ref Guid iid, out IntPtr ppv);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    delegate int CreateDesktopWindowTargetFn(IntPtr self, IntPtr hwnd, [MarshalAs(UnmanagedType.U1)] bool isTopmost, out IntPtr target);

    static IntPtr CallQI(IntPtr punk, Guid iid)
    {
        IntPtr vtbl = Marshal.ReadIntPtr(punk);
        var qi = Marshal.GetDelegateForFunctionPointer<QIFn>(Marshal.ReadIntPtr(vtbl, 0));
        int hr = qi(punk, ref iid, out IntPtr ppv);
        if (hr != 0) Marshal.ThrowExceptionForHR(hr);
        return ppv;
    }

    // WS_EX_NOREDIRECTIONBITMAP 必须在窗口创建时指定（创建后修改无效），故重写 CreateParams
    class OverlayForm : Form
    {
        protected override CreateParams CreateParams
        {
            get
            {
                var cp = base.CreateParams;
                cp.ExStyle |= WS_EX_LAYERED | WS_EX_NOREDIRECTIONBITMAP;
                return cp;
            }
        }

        // Ctrl+Alt+Q 退出、Ctrl+Alt+H 收起/唤出 —— 桌宠出任何问题时都有退路
        protected override void WndProc(ref Message m)
        {
            if (m.Msg == WM_HOTKEY)
            {
                int id = m.WParam.ToInt32();
                if (id == HK_QUIT) { Trace("hotkey quit"); Application.Exit(); return; }
                if (id == HK_TOGGLE) { ToggleVisible(); return; }
                if (id == HK_LAYER)  { SetLayer(!backLayer); return; }
            }
            base.WndProc(ref m);
        }
    }

    static Form form;
    static CoreWebView2CompositionController cc;
    static Windows.UI.Composition.Compositor compositor;   // 保活，防止 GC
    static Windows.UI.Composition.ContainerVisual rootVisual;
    static DesktopWindowTarget dwtTarget;
    static bool clickThrough = false;   // 初始为 false，确保启动时真正写入 WS_EX_TRANSPARENT
    static bool pollBusy;
    static HookProc mouseProc;          // 委托保活，防止被 GC
    static IntPtr mouseHook;
    static bool hitCached, hitBusy, consumedDown;
    static long lastHitTick, hitTick, lastMoveTick;

    // ---- 叼走鼠标指针（Desktop Goose 那套把戏）----
    // 页面每帧把指针"拽"到宠物嘴边，靠的是真的 SetCursorPos。
    // 只要用户自己动一下鼠标、或者点一下，立刻松口 —— 绝不抢控制权。
    static volatile bool grabbing;
    static int lastWarpX = int.MinValue, lastWarpY = int.MinValue;
    static long lastUserMoveTick = Environment.TickCount64;
    static uint browserPid;              // 已弃用（改用进程树扫描），保留占位
    static bool hiddenByUser;

    // 物理像素 → 页面 CSS 像素（WebView2 的视口 = 客户区 / DPI 比例）
    static double DpiScale()
    {
        int dpi = (form != null && !form.IsDisposed) ? form.DeviceDpi : 96;
        return dpi <= 0 ? 1.0 : dpi / 96.0;
    }
    static Point ToPage(Point phys)
    {
        double s = DpiScale();
        return new Point((int)Math.Round(phys.X / s), (int)Math.Round(phys.Y / s));
    }

    // 钩子回调里绝不写文件（I/O 会让钩子超时被系统跳过），先入内存队列，由定时器落盘
    static readonly Queue<string> logQueue = new Queue<string>();
    static void LogClick(string m)
    {
        if (!TraceClick) return;
        lock (logQueue) { if (logQueue.Count < 400) logQueue.Enqueue(m); }
    }
    // 同步命中矩形：页面上报的可交互区域，点击判定在本地完成（无异步、无状态残留）
    static readonly List<Rectangle> hitRects = new List<Rectangle>();
    static bool menuOpen;

    static void ParseRects(string s)
    {
        var list = new List<Rectangle>();
        bool anyMenu = false;
        int dropped = 0;
        // 硬保险：任何明显异常的大矩形（会把整屏点击吞掉）一律丢弃
        int cw = form != null && !form.IsDisposed ? form.ClientSize.Width : 1920;
        int ch = form != null && !form.IsDisposed ? form.ClientSize.Height : 1080;
        int maxArea = Math.Max(300 * 300, cw * ch / 2);
        foreach (var part in s.Split(';'))
        {
            if (string.IsNullOrEmpty(part)) continue;
            int colon = part.IndexOf(':');
            if (colon < 0) continue;
            string tag = part.Substring(0, colon);
            var n = part.Substring(colon + 1).Split(',');
            if (n.Length == 4 && int.TryParse(n[0], out int x) && int.TryParse(n[1], out int y)
                && int.TryParse(n[2], out int w) && int.TryParse(n[3], out int h) && w > 0 && h > 0)
            {
                if ((long)w * h > maxArea) { dropped++; continue; }
                list.Add(new Rectangle(x, y, w, h));
                if (tag == "menu") anyMenu = true;
            }
        }
        lock (hitRects) { hitRects.Clear(); hitRects.AddRange(list); }
        bool changed = menuOpen != anyMenu;
        menuOpen = anyMenu;

        // 首次收到就记一条（不刷屏），便于排查"哪来的墙"
        if (!rectsLogged)
        {
            rectsLogged = true;
            Trace("rects[first] kept=" + list.Count + " dropped=" + dropped + " raw=" + s);
        }
        if (TraceRects) Trace("rects kept=" + list.Count + " raw=" + s);
        if (TraceClick && changed)
            Trace("  menu " + (anyMenu ? "OPEN" : "closed") + " rects=" + s);
    }

    static bool HitTestSync(Point c)
    {
        lock (hitRects)
            foreach (var r in hitRects)
                if (c.X >= r.X && c.X < r.X + r.Width && c.Y >= r.Y && c.Y < r.Y + r.Height)
                    return true;
        return false;
    }

    // 点击追踪默认关闭；设 PETHUB_TRACE_CLICK=1 可开启（排查"哪层窗在吃点击"时用）
    static readonly bool TraceClick = Environment.GetEnvironmentVariable("PETHUB_TRACE_CLICK") == "1";
    static readonly bool TraceRects = Environment.GetEnvironmentVariable("PETHUB_TRACE_RECTS") == "1";
    static bool rectsLogged;

    static void Trace(string m)
    {
        try
        {
            string f = Path.Combine(Path.GetTempPath(), "pethub-light.log");
            var fi = new FileInfo(f);
            if (fi.Exists && fi.Length > 512 * 1024) File.WriteAllText(f, "");   // 防止无限增长
            File.AppendAllText(f, DateTime.Now.ToString("HH:mm:ss.fff ") + m + "\n");
        }
        catch { }
    }

    [STAThread]
    static void Main(string[] args)
    {
        // --watch-app <exe>：给安装器/脚本用的"静默导入"。
        // 只写名单不弹界面；已经在跑的那只会在 2 秒内自己发现新条目。
        CurLang = LoadLang();
        HandleCliImport(args);
        // --quit：叫已经在跑的那只优雅退出（安装器/卸载脚本用）。
        foreach (var a in args)
            if (string.Equals(a, "--quit", StringComparison.OrdinalIgnoreCase))
            {
                SingleInstance.RequestQuit();
                return;
            }
        // 单实例：多个桌宠同时跑 = 多个全屏窗口 + 多个鼠标钩子，会互相抢点击
        // （用户多点几次快捷方式就会变成这样，表现就是"点哪都没反应"）
        if (!SingleInstance.Acquire())
        {
            SingleInstance.PokeOther();     // 再点一次图标 = 给页面发信号，再来一只
            Trace("another instance is running -> poke it (spawn one more pet) and exit");
            return;
        }
        AppDomain.CurrentDomain.UnhandledException += (_, e) => Trace("UNHANDLED: " + e.ExceptionObject);
        Application.ThreadException += (_, e) => Trace("THREAD: " + e.Exception);
        // 必须在创建任何窗口之前声明 DPI 感知：
        // 否则系统把 1920x1200 虚拟成 1536x960 给本进程，钩子拿到的物理坐标
        // 与页面 CSS 坐标差 1.25 倍，宠物永远判定不中，画面也会被系统拉伸发虚。
        try { Application.SetHighDpiMode(HighDpiMode.PerMonitorV2); } catch { }
        Trace("start (composition)");
        StartBridgeIfNeeded();
        // 通用接入：已经导入过的应用，开机就接着盯（名单在 %LOCALAPPDATA%\PetHub\apps.json）
        LoadApps();
        if (watchedApps.Count > 0) StartAppWatchIfNeeded();

        // 多屏自适应：默认铺在光标所在屏幕
        form = new OverlayForm
        {
            FormBorderStyle = FormBorderStyle.None,
            StartPosition = FormStartPosition.Manual,
            Bounds = ScreenBounds(),
            ShowInTaskbar = false,
            TopMost = true,
            Text = "PetHub",
        };
        form.Shown += (_, _) =>
        {
            InitWebView();
            Trace("shown: openAiOnStart=" + openAiOnStart);
            if (openAiOnStart) form.BeginInvoke(new Action(ShowAiConnect));
            // 上次选的层要接着用（默默切，不用再存一次）
            try { if (LoadSetting("layer") == "back") form.BeginInvoke(new Action(() => SetLayer(true, false))); } catch { }
        };

        // 自适应：分辨率变化、屏幕增删、DPI 变化时重排
        Microsoft.Win32.SystemEvents.DisplaySettingsChanged += (_, _) => RefitWindow();
        form.DpiChanged += (_, _) => RefitWindow();

        // 输入模型：窗口始终穿透（桌面随时可点），由全局钩子在命中宠物/HUD 时转发
        InstallMouseHook();
        // 救命热键（必须早于 WebView2 初始化）
        form.HandleCreated += (_, _) =>
        {
            RegisterHotKey(form.Handle, HK_QUIT, MOD_CONTROL | MOD_ALT, (uint)Keys.Q);
            RegisterHotKey(form.Handle, HK_TOGGLE, MOD_CONTROL | MOD_ALT, (uint)Keys.H);
            RegisterHotKey(form.Handle, HK_LAYER, MOD_CONTROL | MOD_ALT, (uint)Keys.B);
            Trace("hotkeys registered (Ctrl+Alt+Q quit, Ctrl+Alt+H toggle, Ctrl+Alt+B layer)");
        };
        StartUsageTracker();
        // 任务栏托盘：宠物可以收起，但"有几个任务在跑、有没有事等我拍板"这件事
        // 得一直看得见——这正是通知区该干的活。
        StartTray();
        Application.ApplicationExit += (_, _) => { StopTray(); StopChildProcesses(); };
        SingleInstance.WatchQuit(() =>
        {
            Trace("quit requested (external)");
            try
            {
                if (form != null && !form.IsDisposed)
                    form.BeginInvoke(new Action(() =>
                    {
                        StopTray();
                        StopChildProcesses();
                        Application.Exit();
                    }));
                else Application.Exit();
            }
            catch { }
        });
        SingleInstance.WatchPoke(() =>
        {
            Trace("poke received");
            try
            {
                form.BeginInvoke(new Action(() =>
                {
                    var core = cc?.CoreWebView2;
                    Trace("poke -> core=" + (core != null));
                    if (core == null) return;
                    try
                    {
                        // 把页面里的异常抓出来：ExecuteScriptAsync 对 JS 异常只会返回 "null"，
                        // Task 不会 faulted，所以必须在 JS 侧包 try/catch 并把结果带回来。
                        const string js =
                            "(function(){try{" +
                            "if(!window.pethubSpawn)return 'NOFN';" +
                            "var b=window.pethubCount?window.pethubCount():-1;" +
                            "window.pethubSpawn();" +
                            "var a=window.pethubCount?window.pethubCount():-1;" +
                            "return 'OK '+b+'->'+a;" +
                            "}catch(e){return 'ERR '+e.message+' @'+String(e.stack).split('\\n')[1];}})()";
                        core.ExecuteScriptAsync(js).ContinueWith(t2 => Trace("spawn -> " + (t2.IsFaulted
                            ? "FAULT " + t2.Exception?.GetBaseException().Message : t2.Result)));
                        // 300ms 后回读宠物数量，确认页面真的多了一只
                        var tw = new System.Windows.Forms.Timer { Interval = 300 };
                        tw.Tick += (_, _) =>
                        {
                            tw.Stop(); tw.Dispose();
                            try
                            {
                                core.ExecuteScriptAsync("String(window.pethubCount ? window.pethubCount() : -1)")
                                    .ContinueWith(t3 => Trace("pets now = " + (t3.IsFaulted
                                        ? "ERR " + t3.Exception?.GetBaseException().Message : t3.Result)));
                            }
                            catch (Exception ex) { Trace("count script EX: " + ex.Message); }
                        };
                        tw.Start();
                    }
                    catch (Exception ex) { Trace("spawn script EX: " + ex.Message); }
                }));
            }
            catch (Exception ex) { Trace("BeginInvoke EX: " + ex.Message); }
        });
        Application.Run(form);
    }

    // ---------------- 初始化 WebView2（Composition 模式） ----------------
    static async void InitWebView()
    {
        try
        {
            Trace("creating env");
            var env = await CoreWebView2Environment.CreateAsync();
            Trace("env ok, creating composition controller");
            cc = await env.CreateCoreWebView2CompositionControllerAsync(form.Handle);
            Trace("controller ok");
        }
        catch (Exception ex)
        {
            Trace("webview init FAIL: " + ex);
            MessageBox.Show("WebView2 初始化失败（系统缺少 WebView2 运行时）：" + ex.Message);
            Application.Exit();
            return;
        }

        try
        {
            // 分层窗口（alpha 由 DComp 逐像素提供；NOREDIRECTIONBITMAP 已在创建时指定）
            SetLayeredWindowAttributes(form.Handle, 0, 255, LWA_ALPHA);

            // 桌面合成目标（先建 DispatcherQueue，再建 Compositor）
            var dqo = new DispatcherQueueOptions { dwSize = Marshal.SizeOf<DispatcherQueueOptions>(), threadType = 2, apartmentType = 2 };
            CreateDispatcherQueueController(dqo, out IntPtr _dq);
            Trace("dispatcher queue ok");
            compositor = new Windows.UI.Composition.Compositor();
            Trace("compositor ok");
            // .NET 10 禁用了 IInspectable 封送，直接手动走 vtable 调 COM（不经过任何封送器）
            Guid iidInterop = new Guid("29E691FA-4567-4DCA-B319-D0F207EB6807");   // SDK 头文件实测值
            IntPtr nativeCompositor = ((IWinRTObject)compositor).NativeObject.ThisPtr;
            IntPtr pInterop = CallQI(nativeCompositor, iidInterop);
            IntPtr vtbl = Marshal.ReadIntPtr(pInterop);
            var createTarget = Marshal.GetDelegateForFunctionPointer<CreateDesktopWindowTargetFn>(
                Marshal.ReadIntPtr(vtbl, 3 * IntPtr.Size));   // 接口继承 IUnknown：槽位 0-2 是 QI/AddRef/Release，3 = CreateDesktopWindowTarget
            int hr = createTarget(pInterop, form.Handle, true, out IntPtr rawTarget);
            Marshal.Release(pInterop);
            if (hr != 0) Marshal.ThrowExceptionForHR(hr);
            var target = MarshalInterface<DesktopWindowTarget>.FromAbi(rawTarget);
            Marshal.Release(rawTarget);
            // target.Root 初始为 null：必须创建容器 Visual 挂上，否则内容不渲染（白屏）
            rootVisual = compositor.CreateContainerVisual();
            target.Root = rootVisual;
            dwtTarget = target;
            cc.RootVisualTarget = rootVisual;
            Trace("composition target ok");

            cc.Bounds = new Rectangle(0, 0, form.ClientSize.Width, form.ClientSize.Height);
            cc.RasterizationScale = form.DeviceDpi / 96f;   // 高分屏按 DPI 渲染
            cc.IsVisible = true;
            cc.DefaultBackgroundColor = Color.Transparent;   // 网页透明直达桌面

            var core = cc.CoreWebView2;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.IsZoomControlEnabled = false;
            core.Settings.AreDevToolsEnabled = false;
            core.WebMessageReceived += OnMessage;

            // 注入轻量版桥：页面识别为"壳环境"（不加深色浏览器底色）；
            // pethubHit(x,y) 供壳层轮询光标做命中测试（穿透模式下页面收不到鼠标事件）
            core.AddScriptToExecuteOnDocumentCreatedAsync(
                "window.petHubLang = '" + LoadLang() + "';" +
                "window.petHubLayer = '" + (LoadSetting("layer") == "back" ? "back" : "front") + "';" +
                "window.petHubInstalledAt = " + LoadInstalledAt() + ";" +
                // 开发期手感动量：PETHUB_DBG=1 启动时页面会把拖拽偏移写进日志
                "window.PETHUB_DBG = " + (Environment.GetEnvironmentVariable("PETHUB_DBG") == "1" ? "true" : "false") + ";" +
                "window.petHubState = " + System.Text.Json.JsonSerializer.Serialize(LoadState()) + ";" +
                "window.petHub = { light: true, setInteractive: h => window.chrome?.webview?.postMessage(h ? 'hit:1' : 'hit:0')," +
                "  setRects: s => window.chrome?.webview?.postMessage('rects:' + s)," +
                "  trace: s => window.chrome?.webview?.postMessage('trace:' + s)," +
                "  tasks: s => window.chrome?.webview?.postMessage('tasks:' + s)," +
                "  setLang: l => window.chrome?.webview?.postMessage('lang:' + l)," +
                "  setLayer: b => window.chrome?.webview?.postMessage('layer:' + (b ? 'back' : 'front'))," +
                "  saveState: s => window.chrome?.webview?.postMessage('state:' + s) };" +
                "window.pethubHit = (x, y) => {" +
                "  const el = document.elementFromPoint(x, y);" +
                "  const overUI = !!(el && el.closest && el.closest('#hud,#menu,#bubble,#toasts'));" +
                "  return (overUI || hitPet(x, y) || hitPoop(x, y) >= 0) ? 1 : 0;" +
                "};");

            string src = FindAppHtml();
            Trace("navigating " + src);
            cc.CoreWebView2.Navigate(src);
            SetClickThrough(true);
            Trace("ready");

            // WebView2 的内部子窗口 + 浏览器进程的宿主窗口都要穿透：否则它们会悄悄吃掉整屏点击
            MakeWebViewChildClickThrough();
            MakeBrowserHostClickThrough();
            // 导航/尺寸/DPI 变化时 WebView2 会重建窗口，定期重新应用（1.5 秒一次，开销可忽略）
            var keepTransparent = new System.Windows.Forms.Timer { Interval = 1500 };
            keepTransparent.Tick += (_, _) =>
            {
                MakeWebViewChildClickThrough();
                MakeBrowserHostClickThrough();
            };
            keepTransparent.Start();

            Trace("client=" + form.ClientSize.Width + "x" + form.ClientSize.Height
                  + " dpi=" + form.DeviceDpi + " scale=" + DpiScale());

            // 点击追踪落盘（钩子内只入队，定时器写文件）
            var logTimer = new System.Windows.Forms.Timer { Interval = 1500 };
            logTimer.Tick += (_, _) =>
            {
                string[] items;
                lock (logQueue)
                {
                    if (logQueue.Count == 0) return;
                    items = logQueue.ToArray();
                    logQueue.Clear();
                }
                foreach (var it in items) Trace(it);
            };
            logTimer.Start();
        }
        catch (Exception ex)
        {
            Trace("composition FAIL: " + ex);
        }
    }

    // ---------------- 鼠标钩子 ----------------
    static void InstallMouseHook()
    {
        mouseProc = MouseHookProc;
        mouseHook = SetWindowsHookEx(WH_MOUSE_LL, mouseProc, IntPtr.Zero, 0);
        Trace("mouse hook " + (mouseHook == IntPtr.Zero ? "FAIL" : "installed"));
    }

    // 让 WebView2 建立的所有子窗口也穿透（父窗口穿透不代表子窗口穿透）
    static void MakeWebViewChildClickThrough()
    {
        EnumChildWindows(form.Handle, (h, _) =>
        {
            int st = GetWindowLong(h, GWL_EXSTYLE);
            if ((st & WS_EX_TRANSPARENT) == 0)      // 幂等：只对新出现/漏掉的子窗口动手
            {
                SetWindowLong(h, GWL_EXSTYLE, st | WS_EX_TRANSPARENT);
                var sb = new System.Text.StringBuilder(128);
                GetClassName(h, sb, 128);
                Trace("child transparent: " + sb);
            }
            return true;
        }, IntPtr.Zero);
    }

    // ---- 真正的"透明墙"：WebView2 在浏览器进程里另有一个铺满全屏的宿主窗口 ----
    // 它不在我们的进程里，EnumChildWindows 找不到，也不受本窗口 WS_EX_TRANSPARENT 影响，
    // 所以桌面点击全被它吃掉。实测可以跨进程给它补 WS_EX_TRANSPARENT（权限足够时直接生效）。
    static HashSet<uint> childPids = new HashSet<uint>();
    static long lastPidScan;
    static readonly Dictionary<uint, uint> procTree = new Dictionary<uint, uint>();

    // 本进程拉起的全部子进程（msedgewebview2.exe 们）：只有它们才可能是我们的宿主窗口
    static void ScanChildProcesses()
    {
        long now = Environment.TickCount64;
        if (childPids.Count > 0 && now - lastPidScan < 5000) return;
        lastPidScan = now;
        procTree.Clear();
        IntPtr snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if (snap == IntPtr.Zero || snap == new IntPtr(-1)) return;
        try
        {
            var e = new PROCESSENTRY32W();
            e.dwSize = (uint)Marshal.SizeOf(typeof(PROCESSENTRY32W));
            if (Process32FirstW(snap, ref e))
                do { procTree[e.th32ProcessID] = e.th32ParentProcessID; }
                while (Process32NextW(snap, ref e));
        }
        finally { CloseHandle(snap); }

        var set = new HashSet<uint>();
        uint self = (uint)Environment.ProcessId;
        foreach (var kv in procTree)
        {
            uint p = kv.Key, guard = 0;
            while (p != 0 && guard++ < 24)
            {
                if (p == self) { set.Add(kv.Key); break; }
                if (!procTree.TryGetValue(p, out uint parent)) break;
                p = parent;
            }
        }
        var old = childPids;
        childPids = set;
        if (set.Count != old.Count || !set.SetEquals(old))
            Trace("webview pids=" + string.Join(",", set));
    }

    static bool IsOurWebViewWindow(IntPtr h)
    {
        var sb = new System.Text.StringBuilder(64);
        GetClassName(h, sb, 64);
        if (!sb.ToString().StartsWith("Chrome_WidgetWin")) return false;
        GetWindowThreadProcessId(h, out uint pid);
        if (pid != 0 && childPids.Contains(pid)) return true;

        // 兜底：WebView2 若复用了别的浏览器进程，进程树里就查不到它。
        // 此时按"与主窗口严丝合缝的合成宿主"识别（三层样式组合 + 尺寸吻合），
        // 普通应用窗口不会同时满足这些条件，不会误伤。
        int ex = GetWindowLong(h, GWL_EXSTYLE);
        if ((ex & WS_EX_LAYERED) == 0 || (ex & WS_EX_NOREDIRECTIONBITMAP) == 0) return false;
        if ((ex & WS_EX_NOACTIVATE) == 0) return false;
        if (form == null || form.IsDisposed) return false;
        if (!GetWindowRect(h, out RECT wr)) return false;
        var fb = form.Bounds;
        return Math.Abs(wr.Left - fb.Left) <= 4 && Math.Abs(wr.Top - fb.Top) <= 4
               && Math.Abs((wr.Right - wr.Left) - fb.Width) <= 4
               && Math.Abs((wr.Bottom - wr.Top) - fb.Height) <= 4;
    }

    static void MakeBrowserHostClickThrough()
    {
        if (form == null || form.IsDisposed) return;
        try
        {
            ScanChildProcesses();
            int fixedCount = 0;
            EnumWindows((h, _) =>
            {
                if (!IsOurWebViewWindow(h)) return true;
                int ex = GetWindowLong(h, GWL_EXSTYLE);
                if ((ex & WS_EX_TRANSPARENT) != 0) return true;           // 已经透了
                SetWindowLong(h, GWL_EXSTYLE, ex | WS_EX_TRANSPARENT);
                int after = GetWindowLong(h, GWL_EXSTYLE);
                if ((after & WS_EX_TRANSPARENT) != 0) fixedCount++;
                var sb = new System.Text.StringBuilder(64);
                GetClassName(h, sb, 64);
                Trace("host transparent: " + sb + " hwnd=" + h + " 0x" + ex.ToString("X8")
                      + " -> 0x" + after.ToString("X8") + ((after & WS_EX_TRANSPARENT) != 0 ? " OK" : " FAILED"));
                return true;
            }, IntPtr.Zero);

            // 自查：本窗口的穿透位也必须还在（被系统/外部工具改掉就补回来）
            if (form != null && !form.IsDisposed)
            {
                int st = GetWindowLong(form.Handle, GWL_EXSTYLE);
                if ((st & WS_EX_TRANSPARENT) == 0)
                {
                    SetWindowLong(form.Handle, GWL_EXSTYLE, st | WS_EX_TRANSPARENT);
                    Trace("self-heal: root transparent restored");
                }
            }
            if (fixedCount > 0) Trace("host transparent applied to " + fixedCount + " window(s)");
        }
        catch (Exception ex) { Trace("host transparent FAIL: " + ex.Message); }
    }

    // 热键：收起 / 唤出桌宠（收起时同时卸掉钩子，桌面 100% 干净）
    static void ToggleVisible()
    {
        try
        {
            if (form == null || form.IsDisposed) return;
            hiddenByUser = !hiddenByUser;
            if (hiddenByUser)
            {
                if (mouseHook != IntPtr.Zero) { UnhookWindowsHookEx(mouseHook); mouseHook = IntPtr.Zero; }
                form.Hide();
                Trace("hotkey: hidden");
            }
            else
            {
                form.Show();
                if (mouseHook == IntPtr.Zero) InstallMouseHook();
                MakeBrowserHostClickThrough();
                Trace("hotkey: shown");
            }
        }
        catch { }
    }

    static IntPtr MouseHookProc(int nCode, IntPtr wParam, IntPtr lParam)
    {
        try
        {
            if (nCode >= 0 && form != null && cc?.CoreWebView2 != null)
            {
                var ms = Marshal.PtrToStructure<MSLLHOOKSTRUCT>(lParam);
                int msg = wParam.ToInt32();
                // —— 指针被叼着的时候：用户一动/一点，立刻松口 ——
                // （自己 SetCursorPos 造成的移动要认出来，否则会自己把自己打断）
                bool isMove = msg == WM_MOUSEMOVE;
                if (grabbing)
                {
                    bool mine = Math.Abs(ms.pt.X - lastWarpX) <= 6 && Math.Abs(ms.pt.Y - lastWarpY) <= 6;
                    if (!isMove || !mine) { grabbing = false; PostToPageAsync("grab:lost"); }
                }
                else lastUserMoveTick = Environment.TickCount64;
                // 钩子给的是**物理像素**，页面 rects 是**CSS 像素**，两者差一个 DPI 比例
                // （125% 缩放下差 1.25 倍，不换算就永远命中不了宠物）
                var cPhys = form.PointToClient(new Point(ms.pt.X, ms.pt.Y));
                bool inside = cPhys.X >= 0 && cPhys.Y >= 0 &&
                              cPhys.X < form.ClientSize.Width && cPhys.Y < form.ClientSize.Height;
                // 拖拽中即使指针甩出窗口也要继续转发：否则抬手事件进不来，
                // 页面会一直以为"还攥在手里"，宠物永远黏着上一次的坐标。
                if (inside || consumedDown)
                {
                    var c = ToPage(cPhys);
                    // 转发用的坐标夹在客户区内（拖到屏幕边缘时宠物就停在边缘）
                    var cp = new Point(
                        Math.Max(0, Math.Min(form.ClientSize.Width - 1, cPhys.X)),
                        Math.Max(0, Math.Min(form.ClientSize.Height - 1, cPhys.Y)));
                    // 移动：不做 hover 时降到 40ms（省开销）；按住拖动时必须逐条转发。
                    // 以前一律 40ms 节流，一次手势里页面只收到 25Hz 的稀疏点，
                    // 手感就是"宠物黏在鼠标后面、永远差半拍"。
                    if (msg == WM_MOUSEMOVE)
                    {
                        long t = Environment.TickCount64;
                        if (consumedDown || t - lastMoveTick >= 40)
                        {
                            lastMoveTick = t;
                            // 按住拖动时要带上左键：不加的话页面看到的 buttons 永远是 0，
                            // Chromium 也不会把这一串 Move 当成一次连续拖拽
                            SendMouse(CoreWebView2MouseEventKind.Move, cp,
                                      consumedDown ? MouseButtons.Left : MouseButtons.None);
                        }
                    }
                    else if (msg == WM_LBUTTONDOWN || msg == WM_RBUTTONDOWN)
                    {
                        // 纯本地矩形判定（绝不跨进程调用，否则钩子会被系统跳过）
                        if (HitTestSync(c))
                        {
                            consumedDown = true;
                            LogClick("click HIT @ " + c.X + "," + c.Y);
                            SendMouse(msg == WM_RBUTTONDOWN
                                ? CoreWebView2MouseEventKind.RightButtonDown
                                : CoreWebView2MouseEventKind.LeftButtonDown,
                                cp, msg == WM_RBUTTONDOWN ? MouseButtons.Right : MouseButtons.Left);
                            return (IntPtr)1;
                        }
                        if (menuOpen) CloseMenuAsync();      // 点在菜单外 → 收起菜单
                        LogClick("click PASS @ " + c.X + "," + c.Y + " rects=" + hitRects.Count
                                 + (menuOpen ? " menu-open" : ""));
                    }
                    else if (msg == WM_LBUTTONUP || msg == WM_RBUTTONUP)
                    {
                        bool right = msg == WM_RBUTTONUP;
                        // 只有"按下被我们吞掉"的那一次，抬起才吞。
                        // 否则会出现：按下穿透给了桌面、抬起却被吃掉 → 桌面卡在选中/拖拽状态。
                        if (consumedDown)
                        {
                            consumedDown = false;
                            SendMouse(right ? CoreWebView2MouseEventKind.RightButtonUp
                                            : CoreWebView2MouseEventKind.LeftButtonUp,
                                      cp, right ? MouseButtons.Right : MouseButtons.Left);
                            return (IntPtr)1;
                        }
                    }
                    else if (msg == WM_MOUSEWHEEL && inside && HitTestSync(c))
                    {
                        int delta = (short)((ms.mouseData >> 16) & 0xffff);
                        try { cc.SendMouseInput(CoreWebView2MouseEventKind.Wheel,
                            CoreWebView2MouseEventVirtualKeys.None, (uint)delta, cp); } catch { }
                        return (IntPtr)1;
                    }
                }
            }
        }
        catch { }
        return CallNextHookEx(mouseHook, nCode, wParam, lParam);   // 未命中 → 放行给桌面
    }

    // 只在鼠标移动时问页面一次（30ms 节流），点击时用缓存，避免点击卡顿
    static async void QueueHitTest(Point c)
    {
        long now = Environment.TickCount64;
        if (hitBusy || now - lastHitTick < 30) return;
        lastHitTick = now; hitBusy = true;
        try
        {
            var res = await cc.CoreWebView2.ExecuteScriptAsync(
                $"(window.pethubHit ? window.pethubHit({c.X},{c.Y}) : 0)");
            hitCached = res != null && res.Trim().Trim('"') == "1";
            if (hitCached) hitTick = Environment.TickCount64;
        }
        catch { }
        finally { hitBusy = false; }
    }

    static void SendMouse(CoreWebView2MouseEventKind kind, Point p, MouseButtons btn)
    {
        if (cc == null) return;
        var keys = CoreWebView2MouseEventVirtualKeys.None;
        if ((btn & MouseButtons.Left) != 0) keys |= CoreWebView2MouseEventVirtualKeys.LeftButton;
        if ((btn & MouseButtons.Right) != 0) keys |= CoreWebView2MouseEventVirtualKeys.RightButton;
        if ((btn & MouseButtons.Middle) != 0) keys |= CoreWebView2MouseEventVirtualKeys.MiddleButton;
        try { cc.SendMouseInput(kind, keys, 0, p); } catch { }
    }

    // 点击落在菜单之外时通知页面收起菜单（用 BeginInvoke 调度，钩子回调必须立刻返回）
    static void CloseMenuAsync()
    {
        try
        {
            if (form == null || form.IsDisposed) return;
            form.BeginInvoke(new Action(() =>
            {
                try { _ = cc?.CoreWebView2?.ExecuteScriptAsync(
                    "(typeof closeMenu === 'function') ? (closeMenu(), 1) : 0"); } catch { }
            }));
        }
        catch { }
    }

    // ---------------- 穿透开关（仅启动时调用一次：窗口全程保持穿透） ----------------
    static void OnMessage(object s, CoreWebView2WebMessageReceivedEventArgs e)
    {
        try
        {
            string msg = e.TryGetWebMessageAsString();
            // 页面也可以主动声明"我现在/不再可交互"（如菜单展开期间）
            if (msg == "hit:1") { hitCached = true; hitTick = Environment.TickCount64; }
            else if (msg == "hit:0") hitCached = false;
            else if (msg != null && msg.StartsWith("rects:")) ParseRects(msg.Substring(6));
            else if (msg != null && msg.StartsWith("trace:"))
            {
                if (TraceClick) Trace("  page " + msg.Substring(6));
            }
            else if (msg != null && msg.StartsWith("lang:")) SetLang(msg.Substring(5));
            else if (msg == "cursor?") SendCursorInfo();
            else if (msg != null && msg.StartsWith("warp:")) WarpCursor(msg.Substring(5));
            else if (msg == "release") { if (grabbing) Trace("cursor released"); grabbing = false; }
            else if (msg != null && msg.StartsWith("layer:")) SetLayer(msg.Substring(6) == "back");
            else if (msg != null && msg.StartsWith("tasks:")) ApplyTaskJson(msg.Substring(6), fromPage: true);
            else if (msg != null && msg.StartsWith("state:")) SaveState(msg.Substring(6));
        }
        catch { }
    }

    // ---------------- 叼走鼠标指针 ----------------
    static void PostToPage(string m)
    {
        try { cc?.CoreWebView2?.PostWebMessageAsString(m); } catch { }
    }
    // 钩子回调里不能跨线程碰 WebView，必须甩回 UI 线程（BeginInvoke 本身很便宜）
    static void PostToPageAsync(string m)
    {
        try
        {
            if (form == null || form.IsDisposed) return;
            form.BeginInvoke(new Action(() => PostToPage(m)));
        }
        catch { }
    }
    static void SendCursorInfo()
    {
        try
        {
            if (!GetCursorPos(out POINT p)) return;
            var c = ToPage(new Point(p.X, p.Y));
            long idle = Environment.TickCount64 - lastUserMoveTick;
            PostToPage("cursor:" + c.X + "," + c.Y + "," + idle);
        }
        catch { }
    }
    static void WarpCursor(string s)
    {
        try
        {
            var parts = s.Split(',');
            if (parts.Length < 2) return;
            if (!int.TryParse(parts[0], out int x) || !int.TryParse(parts[1], out int y)) return;
            double sc = DpiScale();
            int wx = (int)Math.Round(x * sc), wy = (int)Math.Round(y * sc);
            if (wx < -4000 || wy < -4000 || wx > 20000 || wy > 20000) return;
            if (!grabbing) Trace("cursor warp start " + wx + "," + wy);
            lastWarpX = wx; lastWarpY = wy;
            grabbing = true;
            SetCursorPos(wx, wy);
        }
        catch { }
    }

    // ---------------- 前后层 ----------------
    // front = 永远置顶（看得见、随时能点）
    // back  = 压到所有普通窗口之下、桌面之上：点开别的应用就看不见它，桌面上还在
    static bool backLayer = false;

    static void SetLayer(bool back, bool save = true)
    {
        if (form == null) return;
        backLayer = back;
        try
        {
            SetWindowPos(form.Handle, back ? HWND_BOTTOM : HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
            Trace("layer -> " + (back ? "back(desktop)" : "front(topmost)"));
        }
        catch (Exception ex) { Trace("SetLayer EX: " + ex.Message); }
        if (save) SaveSetting("layer", back ? "back" : "front");
        try { cc?.CoreWebView2?.PostWebMessageAsString(back ? "layer:back" : "layer:front"); } catch { }
    }

    static void SetClickThrough(bool on)
    {
        if (on == clickThrough || form == null) return;
        clickThrough = on;
        int st = GetWindowLong(form.Handle, GWL_EXSTYLE);
        if (on) st |= WS_EX_TRANSPARENT;
        else st &= ~WS_EX_TRANSPARENT;
        SetWindowLong(form.Handle, GWL_EXSTYLE, st);   // 保持 WS_EX_LAYERED 不动
    }

    // ---------------- 屏幕时间：只统计前台窗口归属的进程，3 秒一次，开销可忽略 ----------------
    static readonly Dictionary<string, double> usage = new();
    static readonly Dictionary<uint, string> pidName = new();
    static string usageDay = "";
    static int usageTicks;

    static void StartUsageTracker()
    {
        var t = new System.Windows.Forms.Timer { Interval = 3000 };
        t.Tick += (_, _) => UsageTick();
        t.Start();
    }

    static string UsageFile(string day) =>
        Path.Combine(Path.GetDirectoryName(SettingsPath())!, "usage-" + day + ".json");

    static void UsageTick()
    {
        try
        {
            var h = GetForegroundWindow();
            if (h == IntPtr.Zero) return;
            GetWindowThreadProcessId(h, out uint pid);
            if (pid == 0) return;
            if (!pidName.TryGetValue(pid, out string? name))
            {
                try { name = System.Diagnostics.Process.GetProcessById((int)pid).ProcessName; }
                catch { name = ""; }
                if (!string.IsNullOrEmpty(name)) pidName[pid] = name!;
            }
            if (string.IsNullOrEmpty(name)) return;

            string day = DateTime.Now.ToString("yyyy-MM-dd");
            if (day != usageDay)
            {
                usageDay = day; usage.Clear();
                try
                {
                    var f = UsageFile(day);
                    if (File.Exists(f))
                    {
                        var d = System.Text.Json.JsonSerializer.Deserialize<Dictionary<string, double>>(File.ReadAllText(f));
                        if (d != null) foreach (var kv in d) usage[kv.Key] = kv.Value;
                    }
                }
                catch { }
                CleanOldUsage();
            }
            usage[name!] = usage.GetValueOrDefault(name!) + 3;

            // 每分钟：落盘 + 把 Top3 推给页面（页面据此说贴心话）
            if (++usageTicks >= 20)
            {
                usageTicks = 0;
                try
                {
                    File.WriteAllText(UsageFile(day),
                        System.Text.Json.JsonSerializer.Serialize(usage));
                }
                catch { }
                try
                {
                    if (cc?.CoreWebView2 != null)
                    {
                        var top = usage.Where(k => k.Value >= 180 && k.Key.ToLower() != "pethublight")
                                       .OrderByDescending(k => k.Value).Take(3)
                                       .Select(k => "{\"n\":" + System.Text.Json.JsonSerializer.Serialize(k.Key) +
                                                    ",\"s\":" + (int)k.Value + "}");
                        cc.CoreWebView2.ExecuteScriptAsync("(window.__usage&&window.__usage([" +
                            string.Join(",", top) + "]))");
                    }
                }
                catch { }
            }
        }
        catch { }
    }

    static void CleanOldUsage()
    {
        try
        {
            var dir = Path.GetDirectoryName(SettingsPath());
            if (dir == null) return;
            foreach (var f in Directory.GetFiles(dir, "usage-*.json"))
                if (File.GetLastWriteTime(f) < DateTime.Now.AddDays(-7)) File.Delete(f);
        }
        catch { }
    }

    // ---------------- 设置持久化（语言等） ----------------
    static string SettingsPath()
    {
        var dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PetHub");
        Directory.CreateDirectory(dir);
        return Path.Combine(dir, "settings.json");
    }
    // 界面语言。菜单、提示气泡、接入窗口都走 T("中文","English")，
    // 页面那边由 window.pethubSetLang 切 I18N。
    static string CurLang = "zh";
    static string T(string zh, string en) => CurLang == "en" ? en : zh;

    static string OnlineText() => CurLang == "en"
        ? "● Online   " + ApiBase + "    loopback only"
        : "● 接口在线   " + ApiBase + "    只监听本机，外部访问不到";

    static string LoadLang()
    {
        try
        {
            var p = SettingsPath();
            if (File.Exists(p))
            {
                var j = System.Text.Json.JsonDocument.Parse(File.ReadAllText(p));
                if (j.RootElement.TryGetProperty("lang", out var v)) return v.GetString() ?? "zh";
            }
        }
        catch { }
        return "zh";
    }
    static string LoadSetting(string key)
    {
        try
        {
            var p = SettingsPath();
            if (File.Exists(p))
            {
                var j = System.Text.Json.JsonDocument.Parse(File.ReadAllText(p));
                if (j.RootElement.TryGetProperty(key, out var v)) return v.GetString() ?? "";
            }
        }
        catch { }
        return "";
    }
    static void SaveLang(string lang) => SaveSetting("lang", lang == "en" ? "en" : "zh");

    static void SaveSetting(string key, string val)
    {
        try
        {
            var d = LoadSettings();
            d[key] = val;
            File.WriteAllText(SettingsPath(), System.Text.Json.JsonSerializer.Serialize(d));
        }
        catch { }
    }

    // 宠物存档：关机后第二天回来，按真实离开时长结算（会饿、会攒脏东西）
    static Dictionary<string, string> LoadSettings()
    {
        try
        {
            var p = SettingsPath();
            if (File.Exists(p))
            {
                var txt = File.ReadAllText(p);
                var d = System.Text.Json.JsonSerializer.Deserialize<Dictionary<string, string>>(txt);
                if (d != null) return d;
            }
        }
        catch { }
        return new Dictionary<string, string>();
    }
    static string LoadState()
    {
        var d = LoadSettings();
        return d.TryGetValue("state", out var s) ? s : "";
    }

    // 安装纪念日：第一次运行写入，之后一直保留
    static long LoadInstalledAt()
    {
        var d = LoadSettings();
        if (d.TryGetValue("installedAt", out var v) && long.TryParse(v, out long t)) return t;
        long now = DateTimeOffset.Now.ToUnixTimeMilliseconds();
        try
        {
            d["installedAt"] = now.ToString();
            File.WriteAllText(SettingsPath(), System.Text.Json.JsonSerializer.Serialize(d));
        }
        catch { }
        return now;
    }
    static void SaveState(string json)
    {
        try
        {
            if (json.Length > 20000) return;                // 异常数据不落盘（多宠物后存档会变大）
            var d = LoadSettings();
            d["state"] = json;
            File.WriteAllText(SettingsPath(), System.Text.Json.JsonSerializer.Serialize(d));
        }
        catch { }
    }

    // ---------------- 辅助 ----------------
    // 覆盖光标所在屏幕的工作区；顶边多出 3px 移出屏幕，隐藏无框窗口的 DWM 顶边伪影
    static Rectangle ScreenBounds()
    {
        Screen s;
        try { s = Screen.FromPoint(Cursor.Position) ?? Screen.PrimaryScreen; }
        catch { s = Screen.PrimaryScreen; }
        var wa = s.WorkingArea;
        return new Rectangle(wa.X, wa.Y - 3, wa.Width, wa.Height + 3);
    }

    // 屏幕/DPI 变化后重新铺满并同步 WebView2 内容尺寸与缩放
    static void RefitWindow()
    {
        if (form == null) return;
        try
        {
            var b = ScreenBounds();
            form.SuspendLayout();
            form.SetBounds(b.X, b.Y, b.Width, b.Height);
            form.ResumeLayout(true);
            if (cc != null)
            {
                cc.Bounds = new Rectangle(0, 0, form.ClientSize.Width, form.ClientSize.Height);
                cc.RasterizationScale = form.DeviceDpi / 96f;
            }
            Trace("refit: " + b.Width + "x" + b.Height + " dpi=" + form.DeviceDpi);
        }
        catch (Exception ex) { Trace("refit FAIL: " + ex.Message); }
    }

    // 宠物页面位置：安装目录 → 程序目录向上找源码 → 源码仓库常见布局
    // （不写死任何用户目录，换机器/换用户名都能跑，也不泄露个人信息）
    static string FindAppHtml()
    {
        string baseDir = AppContext.BaseDirectory;
        var candidates = new List<string>
        {
            Path.Combine(InstallDir(), "app", "index.html"),        // 安装目录
            Path.Combine(baseDir, "app", "index.html"),             // 发布布局
        };
        var dir = new DirectoryInfo(baseDir);
        for (int i = 0; i < 9 && dir != null; i++)      // 逐层向上找源码仓库（publish 目录很深）
        {
            candidates.Add(Path.Combine(dir.FullName, "app", "index.html"));
            candidates.Add(Path.Combine(dir.FullName, "pet-hub", "app", "index.html"));
            dir = dir.Parent;
        }
        foreach (var c in candidates)
            if (File.Exists(c)) return Path.GetFullPath(c);
        return "about:blank";
    }

    // 安装目录：%LOCALAPPDATA%\PetHub（install.ps1 会把文件放这里）
    static string InstallDir() =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PetHub");

    // 自带的 Python 运行时（安装包里带了一份 embeddable 版）。
    // 有它就不依赖用户机器上装没装 Python、装的是不是 32 位、PATH 里有没有，
    // 装到一台干净系统上也能直接跑。
    static string BundledPythonDir()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (int i = 0; i < 4 && dir != null; i++)
        {
            var cand = Path.Combine(dir.FullName, "runtime", "python");
            if (File.Exists(Path.Combine(cand, "python.exe"))) return cand;
            dir = dir.Parent;
        }
        var inInstall = Path.Combine(InstallDir(), "runtime", "python");
        if (File.Exists(Path.Combine(inInstall, "python.exe"))) return inInstall;
        return null;
    }

    // pythonw 不弹黑框，优先；没有就退回 python。
    static string BundledPythonExe()
    {
        var dir = BundledPythonDir();
        if (dir == null) return null;
        var w = Path.Combine(dir, "pythonw.exe");
        return File.Exists(w) ? w : Path.Combine(dir, "python.exe");
    }

    // 跑脚本用：自带 > 系统 pythonw > python > py
    static IEnumerable<string> PythonCandidates()
    {
        var bundled = BundledPythonExe();
        if (bundled != null) yield return bundled;
        yield return "pythonw";
        yield return "python";
        yield return "py";
    }

    // 自己拉起来的桥进程。退出时要一并收掉，否则桌面宠物关掉了，
    // 后台还留着一个 pythonw 占着 8765（卸载时也删不掉目录）。
    static System.Diagnostics.Process bridgeProc;

    static void StartBridgeIfNeeded()
    {
        try
        {
            using var c = new System.Net.Http.HttpClient();
            c.Timeout = TimeSpan.FromSeconds(1);
            var t = c.GetAsync("http://127.0.0.1:8765/api/health");
            if (t.Wait(1200) && t.Result.IsSuccessStatusCode) return;   // 已在跑
        }
        catch { }
        try
        {
            string? bridge = null;
            var tries = new List<string>
            {
                Path.Combine(InstallDir(), "bridge", "server.py"),
                Path.Combine(AppContext.BaseDirectory, "bridge", "server.py"),
            };
            var dir = new DirectoryInfo(AppContext.BaseDirectory);
            for (int i = 0; i < 6 && dir != null; i++)
            {
                tries.Add(Path.Combine(dir.FullName, "bridge", "server.py"));
                tries.Add(Path.Combine(dir.FullName, "pet-hub", "bridge", "server.py"));
                dir = dir.Parent;
            }
            bridge = tries.FirstOrDefault(File.Exists);
            // 自带运行时优先，其次才是 PATH 里的（不同机器安装情况不同）
            if (bridge != null)
                foreach (var py in PythonCandidates())
                    try
                    {
                        bridgeProc = System.Diagnostics.Process.Start(
                            new System.Diagnostics.ProcessStartInfo(py, '"' + bridge + '"')
                        { CreateNoWindow = true, UseShellExecute = false });
                        break;
                    }
                    catch { }
        }
        catch { }
    }

    // ================= 通用接入：把任意软件导进来 =================
    // 三种接法，从省事到精确：
    //   1. 自动识别：WorkBuddy / Qoder 这类支持 hook 的，installer 已经挂好，零配置
    //   2. 导入应用：菜单里选个 exe（这一步就是"导入"），appwatch.py 盯它的
    //      CPU 活动 + 窗口标题变化，判断"它是不是在处理任务"，任何软件都适用
    //   3. 主动上报：软件自己能跑命令的，一行 `pethub report` 最准
    // 三者写进同一个任务池，宠物 / HUD / 托盘看到的完全一致。

    class WatchedApp
    {
        public string name = "";
        public string exe = "";
        public bool enabled = true;
    }

    static List<WatchedApp> watchedApps = new List<WatchedApp>();
    static bool appsLoaded;
    static System.Diagnostics.Process appWatchProc;
    static System.Windows.Forms.Timer appWatchGuard;
    static int appWatchRetry;

    static string PetHubDir() => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PetHub");
    static string AppsPath() => Path.Combine(PetHubDir(), "apps.json");

    static void LoadApps()
    {
        if (appsLoaded) return;
        appsLoaded = true;
        try
        {
            var p = AppsPath();
            if (!File.Exists(p)) return;
            using var doc = System.Text.Json.JsonDocument.Parse(File.ReadAllText(p));
            if (!doc.RootElement.TryGetProperty("watch", out var arr) ||
                arr.ValueKind != System.Text.Json.JsonValueKind.Array) return;
            foreach (var el in arr.EnumerateArray())
            {
                if (el.ValueKind != System.Text.Json.JsonValueKind.Object) continue;
                var a = new WatchedApp
                {
                    name = JStr(el, "name"),
                    exe = JStr(el, "exe").ToLowerInvariant(),
                    enabled = !(el.TryGetProperty("enabled", out var en) &&
                                en.ValueKind == System.Text.Json.JsonValueKind.False),
                };
                if (a.exe.Length == 0) continue;
                if (!a.exe.EndsWith(".exe")) a.exe += ".exe";
                if (a.name.Length == 0) a.name = a.exe.Substring(0, a.exe.Length - 4);
                if (!watchedApps.Exists(x => x.exe == a.exe)) watchedApps.Add(a);
            }
            Trace("apps loaded: " + watchedApps.Count);
        }
        catch (Exception ex) { Trace("apps load FAIL " + ex.Message); }
    }

    static void SaveApps()
    {
        try
        {
            Directory.CreateDirectory(PetHubDir());
            var sb = new System.Text.StringBuilder("{\n  \"watch\": [\n");
            for (int i = 0; i < watchedApps.Count; i++)
            {
                var a = watchedApps[i];
                sb.Append("    {\"name\": ")
                  .Append(System.Text.Json.JsonSerializer.Serialize(a.name))
                  .Append(", \"exe\": ")
                  .Append(System.Text.Json.JsonSerializer.Serialize(a.exe))
                  .Append(", \"enabled\": ").Append(a.enabled ? "true" : "false").Append('}');
                sb.Append(i == watchedApps.Count - 1 ? "\n" : ",\n");
            }
            sb.Append("  ]\n}\n");
            File.WriteAllText(AppsPath(), sb.ToString());
            Trace("apps saved: " + watchedApps.Count);
        }
        catch (Exception ex) { Trace("apps save FAIL " + ex.Message); }
    }

    // 给安装器 / 脚本用的静默导入：PetHubLight.exe --watch-app "C:\...\foo.exe"
    // 只往名单里写一笔，不弹窗。已经在跑的那只会自己发现（观察器每 2 秒重读名单）。
    static bool openAiOnStart;

    static void HandleCliImport(string[] args)
    {
        try
        {
            for (int i = 0; i < args.Length; i++)
                if (string.Equals(args[i], "--ai-connect", StringComparison.OrdinalIgnoreCase))
                    openAiOnStart = true;
            for (int i = 0; i + 1 < args.Length; i++)
            {
                if (!string.Equals(args[i], "--watch-app", StringComparison.OrdinalIgnoreCase)) continue;
                var path = args[i + 1];
                if (!File.Exists(path)) { Trace("cli watch-app: not found " + path); continue; }
                LoadApps();
                var exe = Path.GetFileName(path).ToLowerInvariant();
                if (watchedApps.Exists(x => x.exe == exe)) continue;
                watchedApps.Add(new WatchedApp
                { name = Path.GetFileNameWithoutExtension(path), exe = exe, enabled = true });
                SaveApps();
                Trace("cli watch-app added " + exe);
            }
        }
        catch (Exception ex) { Trace("cli watch-app FAIL " + ex.Message); }
    }

    static string FindScript(string file)
    {
        // 先看 exe 自己所在的目录：装到哪就是哪，这才是"本机真实路径"。
        // （%LOCALAPPDATA%\PetHub 只是默认安装位置，用户改过路径时那里可能是旧副本。）
        var tries = new List<string>
        {
            Path.Combine(AppContext.BaseDirectory, "connectors", file),
            Path.Combine(InstallDir(), "connectors", file),
        };
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (int i = 0; i < 6 && dir != null; i++)
        {
            tries.Add(Path.Combine(dir.FullName, "connectors", file));
            tries.Add(Path.Combine(dir.FullName, "pet-hub", "connectors", file));
            dir = dir.Parent;
        }
        return tries.FirstOrDefault(File.Exists);
    }

    static void StartAppWatchIfNeeded()
    {
        try
        {
            if (appWatchProc != null && !appWatchProc.HasExited) return;
            if (appWatchGuard == null)
            {
                // 观察器是可有可无的旁路程序：挂了不该影响桌宠，但也不能一直躺着。
                appWatchGuard = new System.Windows.Forms.Timer { Interval = 30000 };
                appWatchGuard.Tick += (_, _) =>
                {
                    if (watchedApps.Count == 0 || appWatchRetry >= 3) return;
                    if (appWatchProc != null && !appWatchProc.HasExited) return;
                    appWatchRetry++;
                    Trace("appwatch restart #" + appWatchRetry);
                    StartAppWatchIfNeeded();
                };
                appWatchGuard.Start();
            }
            var script = FindScript("appwatch.py");
            if (script == null) { Trace("appwatch: script not found"); return; }
            foreach (var py in PythonCandidates())
            {
                try
                {
                    appWatchProc = System.Diagnostics.Process.Start(
                        new System.Diagnostics.ProcessStartInfo(py, '"' + script + '"')
                    { CreateNoWindow = true, UseShellExecute = false });
                    Trace("appwatch started (" + py + ") pid=" + (appWatchProc?.Id));
                    return;
                }
                catch { }
            }
            Trace("appwatch: no python found");
        }
        catch (Exception ex) { Trace("appwatch FAIL " + ex.Message); }
    }

    static void ImportApps()
    {
        try
        {
            LoadApps();
            using var dlg = new OpenFileDialog
            {
                Title = T("选择要接入的应用（可多选）", "Pick the app(s) to watch"),
                Filter = T("程序 (*.exe)|*.exe", "Programs (*.exe)|*.exe"),
                Multiselect = true,
                CheckFileExists = true,
            };
            if (dlg.ShowDialog() != DialogResult.OK) return;
            var added = new List<string>();
            foreach (var f in dlg.FileNames)
            {
                var exe = Path.GetFileName(f).ToLowerInvariant();
                if (watchedApps.Exists(x => x.exe == exe)) continue;
                watchedApps.Add(new WatchedApp
                { name = Path.GetFileNameWithoutExtension(f), exe = exe, enabled = true });
                added.Add(Path.GetFileNameWithoutExtension(f));
            }
            if (added.Count == 0)
            {
                Balloon(T("这些应用已经接进来了", "Already watching those"),
                        T("托盘里「更多 → 已接入」可以启用 / 停用。",
                          "Toggle them under More → watched apps."),
                        ToolTipIcon.Info);
                return;
            }
            SaveApps();
            appWatchRetry = 0;
            StartAppWatchIfNeeded();
            string joined = string.Join(T("、", ", "), added);
            Balloon(T("已接入 " + joined, "Watching " + joined),
                    T("它一开始处理任务，宠物就会说话、托盘也会亮起来。",
                      "When it starts working, the pet speaks up and the tray lights up."),
                    ToolTipIcon.Info);
            SayOnPage(T("记下了，我会一直盯着 " + joined, "Noted — I'll keep an eye on " + joined));
            Trace("apps imported: " + string.Join(",", added));
        }
        catch (Exception ex) { Trace("import FAIL " + ex.Message); }
    }

    // 让宠物把话说明白：接入成功这种"看得见的反馈"必须由宠物给，
    // 系统气泡很容易被忽略。
    static void SayOnPage(string text)
    {
        var core = cc?.CoreWebView2;
        if (core == null || string.IsNullOrEmpty(text)) return;
        try
        {
            core.ExecuteScriptAsync("window.pethubSay && window.pethubSay(" +
                System.Text.Json.JsonSerializer.Serialize(text) + ")");
        }
        catch { }
    }

    // ================= 给 AI 软件接入：宠物本身就是一个接口 =================
    // 思路：不去改别人家的程序，也不指望它们支持插件。PetHub 在本机开一个
    // HTTP 口子（bridge，开机自启），谁的活谁知道，谁自己来报一声。
    // 这个窗口只干一件事——把"怎么报"讲清楚，并且能一键验证"真的通了"。
    static Form aiWin;

    // 默认的 JSON 编码器会把中文路径转成 \u66F2\u6625… ，又长又没法核对。
    static readonly System.Text.Json.JsonSerializerOptions JsonPlain = new()
    { Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    const string ApiBase = "http://127.0.0.1:8765";
    const string ApiEvents = ApiBase + "/api/events";
    const string ApiHelp = ApiBase + "/api/help";

    // 桥没起来时的兜底文案。桥在的话优先用 /api/help 里的原文，
    // 这样窗口上的字永远跟接口真实行为一致，不会改了一处忘另一处。
    const string FallbackPrompt =
        "你接入了 PetHub 桌面宠物的状态接口，它在本机 http://127.0.0.1:8765/api/events\n\n" +
        "每当你开始一个任务、推进进度、或结束时，用本地命令上报一次：\n\n" +
        "curl.exe --noproxy \"*\" -s -X POST http://127.0.0.1:8765/api/events -H \"Content-Type: application/json\" " +
        "-d \"{\\\"platform\\\":\\\"<你的名字>\\\",\\\"task_id\\\":\\\"<固定ID>\\\",\\\"title\\\":\\\"<任务名>\\\",\\\"status\\\":\\\"running\\\",\\\"progress\\\":<0-100>}\"\n\n" +
        "三个必须注意的点：写成 curl.exe（PowerShell 里 curl 是别名）；带 --noproxy \"*\"（否则被系统代理拦成 502）；" +
        "PowerShell 5.1 会吞掉 JSON 里的双引号，接口已容错，照抄即可。\n\n" +
        "规则：\n" +
        "1. task_id 同一个任务始终用同一个值，更新时别换；\n" +
        "2. 开始发 status=running，要人拍板发 status=waiting 并加 \"ask\":true，完成发 status=done，出错发 status=error；\n" +
        "3. 不要把这段说明复述给我，直接执行命令即可；\n" +
        "4. 上报失败（接口没开）就跳过，不要卡住任务。";

    // 桥不在时的英文兜底（正常情况下窗口读的是 /api/help 里的 ai_prompt_en）
    const string FallbackPromptEn =
        "You are wired to the PetHub desktop-pet status API on this machine: http://127.0.0.1:8765/api/events\n\n" +
        "Whenever you start a task, make progress, or finish, report it once with this command:\n\n" +
        "curl.exe --noproxy \"*\" -s -X POST http://127.0.0.1:8765/api/events -H \"Content-Type: application/json\" " +
        "-d \"{\\\"platform\\\":\\\"<your name>\\\",\\\"task_id\\\":\\\"<stable id>\\\",\\\"title\\\":\\\"<task name>\\\",\\\"status\\\":\\\"running\\\",\\\"progress\\\":<0-100>}\"\n\n" +
        "Three things to watch out for: use curl.exe (not curl), keep --noproxy \"*\", and note that " +
        "Windows PowerShell 5.1 strips the JSON quotes - the server repairs that, so copying the line as-is still works.\n\n" +
        "Rules:\n" +
        "1. Keep task_id stable for the same task;\n" +
        "2. status=running to start, status=waiting + \"ask\":true when a human must decide, done when finished, error on failure;\n" +
        "3. Do not repeat these instructions back to me - just run the command;\n" +
        "4. If reporting fails, skip it. Do not block the task.";

    static string FallbackCurl() =>
        "curl.exe --noproxy \"*\" -s -X POST " + ApiEvents + " -H \"Content-Type: application/json\" " +
        "-d \"{\\\"platform\\\":\\\"豆包\\\",\\\"task_id\\\":\\\"t1\\\",\\\"title\\\":\\\"整理季度报表\\\"," +
        "\\\"status\\\":\\\"running\\\",\\\"progress\\\":40}\"";

    static bool BridgeOnline()
    {
        try
        {
            using var c = new System.Net.Http.HttpClient { Timeout = TimeSpan.FromSeconds(1.2) };
            var t = c.GetAsync(ApiBase + "/api/health");
            return t.Wait(1500) && t.Result.IsSuccessStatusCode;
        }
        catch { return false; }
    }

    static string HelpField(string name)
    {
        try
        {
            using var c = new System.Net.Http.HttpClient { Timeout = TimeSpan.FromSeconds(2) };
            string json = c.GetStringAsync(ApiHelp).GetAwaiter().GetResult();
            using var doc = System.Text.Json.JsonDocument.Parse(json);
            if (doc.RootElement.TryGetProperty(name, out var v) &&
                v.ValueKind == System.Text.Json.JsonValueKind.String)
                return v.GetString();
        }
        catch { }
        return null;
    }

    // 发一条测试事件：宠物马上开口，托盘数字跳一下，4 秒后自己撤掉。
    // 这是"接口真的通了"最直接的说服力——比任何说明文字都管用。
    static void PingBridge(Action<string> report)
    {
        System.Threading.Tasks.Task.Run(() =>
        {
            string result;
            try
            {
                using var c = new System.Net.Http.HttpClient { Timeout = TimeSpan.FromSeconds(4) };
                string body = "{\"platform\":\"自检\",\"task_id\":\"selftest\"," +
                              "\"title\":\"接口通了，你的活我看得见\",\"status\":\"running\"," +
                              "\"progress\":100,\"message\":\"来自 PetHub 自检\"}";
                using var content = new System.Net.Http.StringContent(
                    body, System.Text.Encoding.UTF8, "application/json");
                System.Net.Http.HttpResponseMessage r = null;
                for (int i = 0; i < 2; i++)      // 刚拉起来的桥可能要等一下
                {
                    try
                    {
                        r = c.PostAsync(ApiEvents, content).GetAwaiter().GetResult();
                        break;
                    }
                    catch when (i == 0) { System.Threading.Thread.Sleep(900); }
                }
                result = r != null && r.IsSuccessStatusCode ? "ok" : "HTTP " + (int)(r?.StatusCode ?? 0);
            }
            catch (Exception ex) { result = "fail " + ex.Message; }

            try
            {
                System.Threading.Thread.Sleep(4000);
                using var c2 = new System.Net.Http.HttpClient { Timeout = TimeSpan.FromSeconds(3) };
                string rm = "{\"platform\":\"自检\",\"type\":\"remove\",\"task_id\":\"selftest\"}";
                using var c2c = new System.Net.Http.StringContent(
                    rm, System.Text.Encoding.UTF8, "application/json");
                c2.PostAsync(ApiEvents, c2c).GetAwaiter().GetResult();
            }
            catch { }

            try
            {
                if (form != null && !form.IsDisposed)
                    form.BeginInvoke(new Action(() =>
                    {
                        try { report(result); } catch { }
                    }));
            }
            catch { }
        });
    }

    static void ShowAiConnect()
    {
        try { ShowAiConnectCore(); }
        catch (Exception ex)
        {
            Trace("ShowAiConnect FAIL: " + ex);
            try { aiWin?.Dispose(); } catch { }
            aiWin = null;
        }
    }

    static void ShowAiConnectCore()
    {
        Trace("ShowAiConnect enter");
        if (aiWin != null && !aiWin.IsDisposed) { aiWin.Activate(); return; }

        bool online = BridgeOnline();
        string prompt = (CurLang == "en" ? HelpField("ai_prompt_en") : null)
                        ?? HelpField("ai_prompt")
                        ?? (CurLang == "en" ? FallbackPromptEn : FallbackPrompt);
        string curl = (CurLang == "en" ? HelpField("curl_example_en") : null)
                      ?? HelpField("curl_example") ?? FallbackCurl();
        string mcpPath = FindScript("pethub_mcp.py");
        // command 用本机真实存在的解释器路径（自带运行时的绝对路径），
        // 不写死 "python"——否则目标机器 PATH 里没有 Python，MCP 客户端一启动就报找不到命令。
        string mcpPy = BundledPythonExe() ?? "python";
        string mcp = mcpPath == null
            ? "没找到 connectors\\pethub_mcp.py（安装目录下应该有一份）"
            : "{\"mcpServers\":{\"pethub\":{\"command\":" +
              System.Text.Json.JsonSerializer.Serialize(mcpPy, JsonPlain) + ",\"args\":[" +
              System.Text.Json.JsonSerializer.Serialize(mcpPath, JsonPlain) + "]}}}";
        string addr = ApiEvents;

        var fg = Color.FromArgb(255, 230, 237, 246);
        var dim = Color.FromArgb(255, 141, 155, 176);
        var accent = Color.FromArgb(255, 158, 203, 255);

        var f = new Form
        {
            Text = T("PetHub · 给 AI 软件接入", "PetHub · Connect an AI app"),
            FormBorderStyle = FormBorderStyle.FixedSingle,
            MaximizeBox = false,
            MinimizeBox = false,
            StartPosition = FormStartPosition.CenterScreen,
            ClientSize = new Size(780, 648),
            BackColor = Color.FromArgb(255, 15, 19, 25),
            ForeColor = fg,
            TopMost = true,
            ShowInTaskbar = true,
            Font = new Font("Microsoft YaHei UI", 9f),
        };
        aiWin = f;

        Label Lbl(string text, int x, int y, int w, int h, float sz, bool bold, Color c)
        {
            var l = new Label
            {
                Text = text,
                Left = x, Top = y, Width = w, Height = h,
                ForeColor = c,
                BackColor = Color.Transparent,
                AutoSize = false,
                Font = new Font("Microsoft YaHei UI", sz, bold ? FontStyle.Bold : FontStyle.Regular),
            };
            f.Controls.Add(l);
            return l;
        }

        TextBox Box(string text, int x, int y, int w, int h, bool multi)
        {
            var b = new TextBox
            {
                // TextBox 只认 CRLF：从接口拿到的 \n 直接塞进去，段落空行会被吞掉，
                // 一整段说明就糊成一片没法读。
                Text = (text ?? "").Replace("\r\n", "\n").Replace("\n", "\r\n"),
                Left = x, Top = y, Width = w, Height = h,
                Multiline = multi,
                TabStop = false,          // 否则一打开就全选高亮，像坏了一样
                ReadOnly = true,
                AutoSize = false,
                BorderStyle = BorderStyle.FixedSingle,
                BackColor = Color.FromArgb(255, 20, 26, 36),
                ForeColor = Color.FromArgb(255, 207, 228, 255),
                Font = new Font("Consolas", 9.5f),
                WordWrap = multi,
                ScrollBars = multi ? ScrollBars.Vertical : ScrollBars.None,
            };
            f.Controls.Add(b);
            return b;
        }

        Button Btn(string text, int x, int y, int w, int h, Action onClick, Color? bg = null)
        {
            var b = new Button
            {
                Text = text,
                Left = x, Top = y, Width = w, Height = h,
                FlatStyle = FlatStyle.Flat,
                BackColor = bg ?? Color.FromArgb(255, 30, 39, 53),
                ForeColor = fg,
                Font = new Font("Microsoft YaHei UI", 9f),
                Cursor = Cursors.Hand,
            };
            b.FlatAppearance.BorderColor = Color.FromArgb(255, 46, 58, 76);
            b.Click += (_, _) => { try { onClick(); } catch { } };
            f.Controls.Add(b);
            return b;
        }

        // 复制按钮：点完变成"已复制"，1.4 秒后自己变回去。
        // 不弹提示框——这种操作不该打断人。
        Button CopyBtn(int x, int y, int w, string what, Func<string> get)
        {
            // 初始文字用调用方给的那份（已按语言取好），不然英文界面里
            // 四个按钮还写着"复制"
            var btn = Btn(what, x, y, w, 28, () => { }, Color.FromArgb(255, 34, 44, 60));
            btn.Click += (_, _) =>
            {
                try
                {
                    Clipboard.SetText(get() ?? "");
                    btn.Text = T("已复制", "Copied");
                    var t = new System.Windows.Forms.Timer { Interval = 1400 };
                    t.Tick += (_, _) =>
                    {
                        t.Stop(); t.Dispose();
                        try { btn.Text = what; } catch { }
                    };
                    t.Start();
                }
                catch { btn.Text = T("复制失败", "Copy failed"); }
            };
            return btn;
        }

        Lbl(T("给 AI 软件接入", "Connect an AI app"), 22, 16, 400, 28, 15f, true, fg);
        Lbl(T("PetHub 自己就是一个接口。谁的活谁知道，谁自己来报一声——不用改你的软件，也不用它支持插件。",
              "PetHub is an endpoint. Whatever is doing the work just reports in — no plugin, no changes."),
            22, 48, 736, 22, 9f, false, dim);

        var green = Color.FromArgb(255, 65, 214, 138);
        var red = Color.FromArgb(255, 216, 74, 80);
        string offLine = T("● 接口没起来（可能是 Python 不在 PATH）。点右边「重试连接」",
                           "● Endpoint is down (Python missing from PATH?). Hit \"Retry\" on the right.");

        // 刚打开时桥可能还在启动（Python 冷启动要一两秒）。这里先显示"连接中"，
        // 拿到结果再变色——否则每次开窗都先红一下，像坏了。
        var status = Lbl(online ? OnlineText() : T("● 正在连接口…", "● Connecting to the endpoint…"),
            22, 78, 736, 20, 9f, false, online ? green : dim);

        Lbl(T("① 地址 —— 任何能发 HTTP 请求的软件都能用",
              "① Endpoint — anything that can POST can use it"), 22, 112, 736, 20, 9f, false, accent);
        Box(addr, 22, 134, 580, 26, false);
        CopyBtn(612, 133, 92, T("复制", "Copy"), () => addr);

        // 每个大框都占满宽度，"复制"按钮统一收到标题行右端——
        // 框里能多看两行，按钮也不会挤在框边上。
        Lbl(T("② 把这段提示词整段粘给 AI 办公软件（豆包 / 百度搭子 / ChatGPT…）",
              "② Paste this prompt into your AI app (Doubao / Baidu Dazi / ChatGPT…)"),
            22, 174, 590, 20, 9f, false, accent);
        var promptBox = Box(prompt, 22, 196, 736, 168, true);
        CopyBtn(600, 172, 160, T("复制提示词", "Copy prompt"), () => promptBox.Text);

        Lbl(T("③ 或者自己拼一行命令，粘到终端里试",
              "③ Or run this one-liner in a terminal"), 22, 376, 590, 20, 9f, false, accent);
        var curlBox = Box(curl, 22, 398, 736, 100, true);
        CopyBtn(600, 374, 160, T("复制命令", "Copy command"), () => curlBox.Text);

        // 路径不是写死的：下面这段是按"本机实际安装位置"现算的，
        // 换台电脑装，里面就是那台电脑的路径。
        Lbl(T("④ 支持 MCP 的客户端（Claude Desktop / Cursor / Cline…）配置里是本机真实路径",
              "④ MCP clients (Claude Desktop / Cursor / Cline…) — real paths on this PC"),
            22, 510, 736, 20, 9f, false, accent);
        // 三行：路径是本机绝对路径，比较长，两行会被切掉尾巴
        var mcpBox = Box(mcp, 22, 532, 736, 62, true);
        CopyBtn(600, 508, 160, T("复制配置", "Copy config"), () => mcpBox.Text);

        // 底部一行：左"测一下"，右"关闭"
        var testBtn = Btn(online ? T("发一条测试，看它说话", "Send a test event")
                                  : T("重试连接", "Retry"), 22, 602, 210, 30, () => { });
        var docBtn = Btn(T("打开说明页", "Open docs"), 242, 602, 120, 30, () =>
        {
            try
            {
                System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(
                    ApiBase + "/help") { UseShellExecute = true });
            }
            catch { }
        });
        var closeBtn = Btn(T("关闭", "Close"), 658, 602, 100, 30, () => f.Close(),
                           Color.FromArgb(255, 40, 52, 70));

        testBtn.Click += (_, _) =>
        {
            if (!BridgeOnline())
            {
                // 桥没起来就先拉一次（安装目录里有 server.py 的话）
                StartBridgeIfNeeded();
                System.Threading.Thread.Sleep(600);
            }
            testBtn.Enabled = false;
            testBtn.Text = T("发送中…", "Sending…");
            SayOnPage("看，我能收到");
            PingBridge(res =>
            {
                try
                {
                    testBtn.Enabled = true;
                    testBtn.Text = res == "ok"
                        ? T("✓ 通了，再发一条", "✓ It works — send another")
                        : T("✗ 没通，看我这句话", "✗ Failed — check my speech bubble");
                    if (res != "ok")
                        Balloon(T("接口没通", "Endpoint unreachable"),
                                T("多半是 Python 不在 PATH，或者 8765 端口被占了。",
                                  "Usually Python missing from PATH, or port 8765 is taken."),
                                ToolTipIcon.Warning);
                    if (res == "ok")
                        SayOnPage(T("收到啦，以后你干什么我都知道", "Got it — I'm watching now"));
                }
                catch { }
            });
        };


        // 接口一通就把绿字和线上那份文案换上：免得用户复制到的是兜底旧文案。
        void ApplyOnline(bool on)
        {
            try
            {
                status.Text = on ? OnlineText() : offLine;
                status.ForeColor = on ? green : red;
                testBtn.Text = on ? T("发一条测试，看它说话", "Send a test event")
                                  : T("重试连接", "Retry");
                if (!on) return;
                var p2 = (CurLang == "en" ? HelpField("ai_prompt_en") : null) ?? HelpField("ai_prompt");
                if (!string.IsNullOrEmpty(p2) && p2 != promptBox.Text)
                {
                    promptBox.Text = p2.Replace("\r\n", "\n").Replace("\n", "\r\n");
                    promptBox.SelectionStart = 0;
                    promptBox.SelectionLength = 0;
                }
                var c2 = (CurLang == "en" ? HelpField("curl_example_en") : null)
                         ?? HelpField("curl_example");
                if (!string.IsNullOrEmpty(c2) && c2 != curlBox.Text)
                {
                    curlBox.Text = c2.Replace("\r\n", "\n").Replace("\n", "\r\n");
                    curlBox.SelectionStart = 0;
                    curlBox.SelectionLength = 0;
                }
            }
            catch { }
        }

        // 打开时桥还没起来的话，自己盯一会儿（约 8 秒），通了就变绿，不用人点。
        System.Windows.Forms.Timer poll = null;
        if (!online)
        {
            int tries = 0;
            poll = new System.Windows.Forms.Timer { Interval = 700 };
            poll.Tick += (_, _) =>
            {
                if (f.IsDisposed) { try { poll.Stop(); poll.Dispose(); } catch { } return; }
                poll.Stop();
                bool ok;
                try { ok = BridgeOnline(); } catch { ok = false; }
                if (ok) { try { poll.Dispose(); } catch { } ApplyOnline(true); return; }
                if (++tries >= 11) { try { poll.Dispose(); } catch { } ApplyOnline(false); return; }
                poll.Start();
            };
            poll.Start();
            f.FormClosed += (_, _) => { try { poll.Stop(); poll.Dispose(); } catch { } };
        }

        f.HandleCreated += (_, _) =>
        {
            try
            {
                int on = 1;
                DwmSetWindowAttribute(f.Handle, 20, ref on, 4);   // Win10 1809+
                DwmSetWindowAttribute(f.Handle, 19, ref on, 4);   // 更老的版本用 19
            }
            catch { }
        };
        f.FormClosed += (_, _) => { Trace("aiWin closed"); aiWin = null; };
        f.FormClosing += (_, e) => Trace("aiWin closing, reason=" + e.CloseReason);
        f.Shown += (_, _) =>
        {
            try { f.ActiveControl = null; } catch { }
            // 只读多行框会"记住"上次滚动位置，打开就停在中间，看着像开头没了。
            foreach (var b in new[] { promptBox, curlBox })
            {
                try { b.SelectionStart = 0; b.SelectionLength = 0; b.ScrollToCaret(); } catch { }
            }
        };
        Trace("aiWin about to show, online=" + online);
        f.Show();
        Trace("aiWin shown visible=" + f.Visible + " hwnd=" + f.Handle + " bounds=" + f.Bounds);
        f.Activate();
    }


    // ================= 任务栏托盘：让"正在处理什么"在通知区也看得见 =================
    // 设计取舍：
    //   1. 数据以页面推送为主（页面那条 SSE 零延迟），壳层 3 秒轮询只当兜底——
    //      页面要是卡住/崩了，托盘不会永远僵在"还在跑"。
    //   2. 图标是现画的，不占图片资源：外环颜色表状态，中间数字表在办几件。
    //   3. 托盘只做"看"和"唤出"，不复制一套功能菜单，免得和宠物自己的菜单打架。

    class TrayTask
    {
        public string Key = "", Platform = "", Title = "", Status = "running", Message = "";
        public int? Progress;
    }

    static NotifyIcon tray;
    static ContextMenuStrip trayMenu;
    static System.Windows.Forms.Timer trayPoll;
    static List<TrayTask> trayTasks = new List<TrayTask>();
    static int trayBusy, trayWait, trayErr, lastWait;
    static string traySig = "";
    static long lastPageReportTick, lastBalloonTick;
    static bool trayPolling;

    [DllImport("user32.dll", SetLastError = true)]
    static extern bool DestroyIcon(IntPtr hIcon);

    [DllImport("dwmapi.dll")]
    static extern int DwmSetWindowAttribute(IntPtr hWnd, int attr, ref int attrValue, int attrSize);

    static void StartTray()
    {
        // 一个进程只能有且只有一个 NotifyIcon。哪怕哪天调用点多了一处，
        // 也绝不能出现两个图标（这是"有时两个有时三个"的另一半来源）。
        if (tray != null) { Trace("tray skip (already exists)"); return; }
        try
        {
            tray = new NotifyIcon { Visible = false, Icon = TrayIconFor(0, 0, 0), Text = "PetHub · 空闲" };
            tray.Visible = true;

            // 进程退出时把自己这个图标摘掉。被任务管理器硬杀的摘不掉，
            // 那种会在通知区留一个点不动的"幽灵图标"（鼠标划过就消失）——
            // 所以安装器/卸载脚本都改成先发 WM_CLOSE，不再一上来就 taskkill /F。
            AppDomain.CurrentDomain.ProcessExit += (_, _) =>
            {
                try { if (tray != null) { tray.Visible = false; tray.Dispose(); tray = null; } } catch { }
            };

            trayMenu = new ContextMenuStrip();
            trayMenu.Opening += (_, _) => FillTrayMenu();
            tray.ContextMenuStrip = trayMenu;

            // 左键单击 = 展开/收起任务面板。任务栏这一侧最常用的动作就是这个。
            tray.MouseClick += (_, e) => { if (e.Button == MouseButtons.Left) ToggleTaskHud(); };

            trayPoll = new System.Windows.Forms.Timer { Interval = 3000 };
            trayPoll.Tick += (_, _) => PollTasksFallback();
            trayPoll.Start();

            TrayFirstRunHint();
            Trace("tray ready");
        }
        catch (Exception ex) { Trace("tray FAIL: " + ex.Message); }
    }

    // Windows 11 会把新图标一股脑塞进溢出面板（点任务栏的 ^ 才看得到），
    // 用户第一反应是"没做出来"。所以第一次运行时主动说一声，并且让宠物也提一句。
    static void TrayFirstRunHint()
    {
        try
        {
            if (LoadSetting("trayHinted") == "1") return;
            SaveSetting("trayHinted", "1");
            var t = new System.Windows.Forms.Timer { Interval = 4000 };
            t.Tick += (_, _) =>
            {
                t.Stop(); t.Dispose();
                try
                {
                    tray.ShowBalloonTip(9000,
                        T("PetHub 已在通知区待命", "PetHub is in the notification area"),
                        T("看不到我？点任务栏的 ^ ，把 PetHub 图标拖到任务栏上就好，以后就一直在那儿。",
                          "Can't see me? Click ^ on the taskbar and drag the PetHub icon onto the taskbar."),
                        ToolTipIcon.Info);
                }
                catch { }
                var core = cc?.CoreWebView2;
                if (core != null)
                    try
                    {
                        core.ExecuteScriptAsync(
                            "window.pethubHintTray && window.pethubHintTray()");
                    }
                    catch { }
            };
            t.Start();
        }
        catch { }
    }

    static void StopTray()
    {
        try { if (tray != null) { tray.Visible = false; tray.Dispose(); tray = null; } } catch { }
    }

    // 把自己拉起的 python 子进程一并结束（只结束"自己启动的那一个"，
    // 桥本来就在跑的话 bridgeProc 是 null，不去动别人）
    static void StopChildProcesses()
    {
        try { if (appWatchProc != null && !appWatchProc.HasExited) appWatchProc.Kill(); } catch { }
        try { if (bridgeProc != null && !bridgeProc.HasExited) bridgeProc.Kill(); } catch { }
        appWatchProc = null;
        bridgeProc = null;
    }

    // ---- 数据进来 ----
    static string JStr(System.Text.Json.JsonElement el, string name, string dflt = "")
        => el.TryGetProperty(name, out var v) && v.ValueKind == System.Text.Json.JsonValueKind.String
           ? (v.GetString() ?? dflt) : dflt;

    static int? JNum(System.Text.Json.JsonElement el, string name)
        => el.TryGetProperty(name, out var v) && v.ValueKind == System.Text.Json.JsonValueKind.Number
           ? (int?)Math.Round(v.GetDouble()) : null;

    static void ApplyTaskJson(string json, bool fromPage)
    {
        try
        {
            if (fromPage) lastPageReportTick = Environment.TickCount64;
            using var doc = System.Text.Json.JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (root.ValueKind != System.Text.Json.JsonValueKind.Object) return;

            var list = new List<TrayTask>();
            if (root.TryGetProperty("tasks", out var arr) && arr.ValueKind == System.Text.Json.JsonValueKind.Array)
            {
                foreach (var el in arr.EnumerateArray())
                {
                    if (el.ValueKind != System.Text.Json.JsonValueKind.Object) continue;
                    var t = new TrayTask();
                    if (fromPage)   // 页面用的是压缩字段（省带宽、省序列化）
                    {
                        t.Platform = JStr(el, "p");
                        t.Key      = JStr(el, "k");
                        t.Title    = JStr(el, "t");
                        t.Status   = JStr(el, "s", "running");
                        t.Message  = JStr(el, "m");
                        t.Progress = JNum(el, "g");
                    }
                    else            // 桥接 /api/state 的原始字段
                    {
                        t.Platform = JStr(el, "platform");
                        t.Key      = t.Platform + "::" + JStr(el, "id");
                        t.Title    = JStr(el, "title");
                        t.Status   = JStr(el, "status", "running");
                        t.Message  = JStr(el, "message");
                        t.Progress = JNum(el, "progress");
                    }
                    if (t.Key.Length == 0) continue;
                    // 同一任务只留一份（页面和轮询可能前后脚到）
                    if (list.Exists(x => x.Key == t.Key)) continue;
                    list.Add(t);
                }
            }
            UpdateTrayUi(list);
        }
        catch (Exception ex) { Trace("tray parse FAIL: " + ex.Message); }
    }

    static void UpdateTrayUi(List<TrayTask> list)
    {
        if (tray == null) return;
        trayTasks = list;

        int wait = 0, busy = 0, err = 0;
        foreach (var t in list)
        {
            if (t.Status == "waiting") wait++;
            else if (t.Status == "running" || t.Status == "thinking") busy++;
            else if (t.Status == "error") err++;
        }
        trayBusy = busy; trayWait = wait; trayErr = err;

        string tip = BuildTrayTip(list, wait, busy, err);
        if (tray.Text != tip) { try { tray.Text = tip; } catch { } }

        // 通知区重复图标的根因就在这里：
        // 以前每次状态变化都 new 一个 Icon（= 新 HICON），再把旧的 Dispose 掉。
        // 资源管理器缓存的是句柄，句柄一换就是另一个图标对象，连续换的时候
        // 通知区会短暂出现 2~3 个"幽灵图标"，过一会儿自己消失——正是用户看到的现象。
        // 现在改成：状态组合固定就那么多种，每种只画一次、之后反复复用同一个实例，
        // 句柄永不变化，也就不会再多出图标。
        string sig = (wait > 0 ? "w" : err > 0 ? "e" : busy > 0 ? "b" : "i")
                   + Math.Min(busy + wait, 10) + "/" + (list.Count > 0);
        if (sig != traySig)
        {
            traySig = sig;
            Trace("tray " + sig + " | " + tip.Replace('\n', ' '));
            QueueTrayIcon(busy, wait, err);
        }

        // 新冒出"等你确认"才冒泡，避免每轮都弹
        if (wait > lastWait)
            Balloon(wait == 1 ? T("有任务在等你确认", "One task is waiting for you")
                              : T($"{wait} 个任务在等你确认", $"{wait} tasks are waiting for you"),
                    FirstTitle(list, "waiting") ?? T("点开看看要拍板什么", "Open it and see what needs a call"),
                    ToolTipIcon.Warning);
        lastWait = wait;
    }

    // ---- 图标缓存：一套状态只画一次 ----
    // 托盘图标只有 4 种颜色 × 11 种数字，全画出来也就 40 个 32x32 位图，
    // 换来的是"句柄恒定"，资源管理器不再以为来了新图标。
    static readonly Dictionary<string, Icon> trayIconCache = new Dictionary<string, Icon>();
    static System.Windows.Forms.Timer trayIconDebounce;
    static int pendingBusy, pendingWait, pendingErr;
    static bool pendingIcon;

    static Icon TrayIconFor(int busy, int wait, int err)
    {
        int n = Math.Min(busy + wait, 10);
        string key = (wait > 0 ? "w" : err > 0 ? "e" : n > 0 ? "b" : "i") + n;
        if (!trayIconCache.TryGetValue(key, out var ic))
        {
            ic = MakeTrayIcon(n >= 10 ? 10 : busy, n >= 10 ? 0 : wait, err);
            trayIconCache[key] = ic;
        }
        return ic;
    }

    // 连着一串状态变化（任务一多，busy 数会在 1 秒内跳好几次）时不要挨个换，
    // 攒 400ms 只应用最后一个，通知区一次都不闪。
    static void QueueTrayIcon(int busy, int wait, int err)
    {
        pendingBusy = busy; pendingWait = wait; pendingErr = err; pendingIcon = true;
        if (trayIconDebounce == null)
        {
            trayIconDebounce = new System.Windows.Forms.Timer { Interval = 400 };
            trayIconDebounce.Tick += (_, _) =>
            {
                if (!pendingIcon) { trayIconDebounce.Stop(); return; }
                pendingIcon = false;
                try
                {
                    if (tray == null) return;
                    var ic = TrayIconFor(pendingBusy, pendingWait, pendingErr);
                    if (!ReferenceEquals(tray.Icon, ic)) tray.Icon = ic;
                }
                catch { }
            };
        }
        trayIconDebounce.Start();     // 重新计时：400ms 内没有新变化才真正切
    }

    static string FirstTitle(List<TrayTask> list, string status)
    {
        foreach (var t in list) if (t.Status == status) return t.Title;
        return null;
    }

    static string Cut(string s, int n)
        => string.IsNullOrEmpty(s) ? "" : (s.Length <= n ? s : s.Substring(0, n - 1) + "…");

    static string BuildTrayTip(List<TrayTask> list, int wait, int busy, int err)
    {
        if (list.Count == 0) return "PetHub · 空闲";
        string head = wait > 0 ? $"等你确认 {wait}"
                    : busy > 0 ? $"进行中 {busy}"
                    : err > 0  ? $"出错 {err}"
                    :            $"已完成 {list.Count}";
        var top = (wait > 0 ? FirstOfStatus(list, "waiting") : null)
               ?? (busy > 0 ? FirstOfStatus(list, "running") : null)
               ?? list[0];
        string body = top.Title;
        if (top.Progress != null) body += " " + top.Progress + "%";
        string tip = "PetHub · " + head + "\n" + body;
        // NotifyIcon.Text 有长度上限，超了直接抛异常
        return tip.Length <= 63 ? tip : tip.Substring(0, 62) + "…";
    }

    static TrayTask FirstOfStatus(List<TrayTask> list, string status)
    {
        foreach (var t in list)
            if (t.Status == status || (status == "running" && t.Status == "thinking")) return t;
        return null;
    }

    static void Balloon(string title, string text, ToolTipIcon icon)
    {
        try
        {
            long now = Environment.TickCount64;
            if (now - lastBalloonTick < 20000) return;   // 20 秒内最多一次，别刷屏
            lastBalloonTick = now;
            tray.ShowBalloonTip(6000, title, Cut(text, 120), icon);
        }
        catch { }
    }

    // ---- 图标：现画，不依赖资源文件 ----
    static Icon MakeTrayIcon(int busy, int wait, int err)
    {
        int n = busy + wait;
        using var bmp = new Bitmap(32, 32);
        using (var g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.AntiAlias;
            g.Clear(Color.Transparent);

            // 环的颜色就是状态：琥珀=等你、红=出错、蓝=在跑、灰=闲
            Color ring = wait > 0 ? Color.FromArgb(255, 255, 176, 32)
                       : err > 0  ? Color.FromArgb(255, 232, 76, 82)
                       : n > 0    ? Color.FromArgb(255, 74, 148, 255)
                       :            Color.FromArgb(255, 122, 136, 158);

            using (var fill = new SolidBrush(Color.FromArgb(255, 24, 30, 42)))
                g.FillEllipse(fill, 1.5f, 1.5f, 29f, 29f);
            using (var pen = new Pen(ring, 3f))
                g.DrawEllipse(pen, 2f, 2f, 28f, 28f);

            if (n > 0)
            {
                string s = n > 9 ? "9+" : n.ToString();
                using var f = new Font("Segoe UI", s.Length > 1 ? 12f : 14f, FontStyle.Bold, GraphicsUnit.Pixel);
                using var tb = new SolidBrush(ring);
                var sz = g.MeasureString(s, f);
                g.DrawString(s, f, tb, 16f - sz.Width / 2f, 16f - sz.Height / 2f + 0.5f);
            }
            else
            {
                using var dot = new SolidBrush(ring);
                g.FillEllipse(dot, 13f, 13f, 6f, 6f);
            }
        }
        IntPtr h = bmp.GetHicon();
        try
        {
            using var tmp = Icon.FromHandle(h);
            return (Icon)tmp.Clone();       // Clone 之后才能安全销毁原句柄
        }
        finally { DestroyIcon(h); }
    }

    // ---- 右键菜单：只列任务 + 几个必要开关 ----
    static ToolStripMenuItem MItem(string text, Action onClick, string keyTip = null)
    {
        var it = new ToolStripMenuItem(text);
        if (onClick != null) it.Click += (_, _) => { try { onClick(); } catch { } };
        if (!string.IsNullOrEmpty(keyTip)) it.ShortcutKeyDisplayString = keyTip;
        return it;
    }

    static void FillTrayMenu()
    {
        if (trayMenu == null) return;
        trayMenu.SuspendLayout();
        trayMenu.Items.Clear();

        // 顶层只留"看一眼 + 常用动作"：标题、摘要、任务、三个动作、更多、退出。
        // 接入相关的明细都收进「更多」，因为那是设一次就不再动的。
        var title = new ToolStripMenuItem("PetHub") { Enabled = false };
        try { title.Font = new Font(title.Font, FontStyle.Bold); } catch { }
        trayMenu.Items.Add(title);

        string summary = trayTasks.Count == 0
            ? T("空闲中", "Idle")
            : T(trayTasks.Count + " 个任务", trayTasks.Count + (trayTasks.Count == 1 ? " task" : " tasks"));
        if (trayWait > 0) summary += T(" · 等确认 " + trayWait, " · " + trayWait + " waiting");
        if (trayErr > 0) summary += T(" · 出错 " + trayErr, " · " + trayErr + " failed");
        trayMenu.Items.Add(new ToolStripMenuItem(summary) { Enabled = false });

        if (trayTasks.Count > 0)
        {
            trayMenu.Items.Add(new ToolStripSeparator());
            int shown = 0;
            foreach (var t in trayTasks)
            {
                if (shown >= 6)
                {
                    trayMenu.Items.Add(new ToolStripMenuItem(
                        T("… 还有 " + (trayTasks.Count - shown) + " 个",
                          "… and " + (trayTasks.Count - shown) + " more")) { Enabled = false });
                    break;
                }
                shown++;
                // 别用 emoji 当状态符：ToolStripMenuItem 的默认字体不认 ❗，
                // 会渲染成一个空心方块，看着像坏了。
                string mark = t.Status == "waiting" ? "!"
                            : t.Status == "done" ? "✓"
                            : t.Status == "error" ? "✗" : "•";
                string pct = t.Progress != null ? "  " + t.Progress + "%" : "";
                var it = new ToolStripMenuItem($"{mark} {Cut(t.Title, 20)}{pct}");
                if (t.Status == "waiting")
                {
                    try { it.Font = new Font(it.Font, FontStyle.Bold); } catch { }
                    it.ForeColor = Color.FromArgb(255, 214, 138, 22);
                }
                else if (t.Status == "error") it.ForeColor = Color.FromArgb(255, 216, 74, 80);
                else if (t.Status == "done") it.ForeColor = Color.FromArgb(255, 116, 132, 152);
                string key = t.Key;
                it.Click += (_, _) => SayTask(key);
                trayMenu.Items.Add(it);
            }
        }

        trayMenu.Items.Add(new ToolStripSeparator());
        trayMenu.Items.Add(MItem(T("显示 / 收起桌宠", "Show / hide pet"), ToggleVisible, "Ctrl+Alt+H"));
        trayMenu.Items.Add(MItem(T("展开 / 收起任务面板", "Toggle task panel"), ToggleTaskHud));

        // "接入"不藏二级：一次点击直接到文件选择框。
        trayMenu.Items.Add(MItem(T("接入应用…", "Add an app…"), ImportApps));

        var more = new ToolStripMenuItem(T("更多", "More"));

        LoadApps();
        if (watchedApps.Count > 0)
        {
            var list = new ToolStripMenuItem(T("已接入 " + watchedApps.Count + " 个",
                                               watchedApps.Count + " app(s) watched"));
            foreach (var app in watchedApps.ToList())
            {
                var a = app;
                var it = new ToolStripMenuItem(a.name + (a.name == a.exe ? "" : "  (" + a.exe + ")"))
                { Checked = a.enabled };
                it.Click += (_, _) =>
                {
                    a.enabled = !a.enabled;
                    SaveApps();
                    Balloon(T((a.enabled ? "已启用 " : "已停用 ") + a.name,
                              (a.enabled ? "Enabled " : "Disabled ") + a.name),
                            a.enabled
                              ? T("它一开始处理任务，我就会告诉你。", "I'll tell you as soon as it starts working.")
                              : T("不再盯着它了。", "I'll stop watching it."),
                            ToolTipIcon.Info);
                };
                list.DropDownItems.Add(it);
            }
            list.DropDownItems.Add(new ToolStripSeparator());
            list.DropDownItems.Add(MItem(T("清空名单", "Clear list"), () =>
            {
                watchedApps.Clear();
                SaveApps();
                Balloon(T("名单已清空", "List cleared"),
                        T("下次「接入应用」重新挑就行。", "Pick again from \"Add an app\" whenever you like."),
                        ToolTipIcon.Info);
            }));
            more.DropDownItems.Add(list);
        }

        more.DropDownItems.Add(MItem(T("给 AI 软件接入…", "Connect an AI app…"), ShowAiConnect));
        more.DropDownItems.Add(new ToolStripSeparator());

        var lang = new ToolStripMenuItem(T("语言 / Language", "语言 / Language"));
        var zh = new ToolStripMenuItem("中文") { Checked = CurLang != "en" };
        var en = new ToolStripMenuItem("English") { Checked = CurLang == "en" };
        zh.Click += (_, _) => SetLang("zh");
        en.Click += (_, _) => SetLang("en");
        lang.DropDownItems.Add(zh);
        lang.DropDownItems.Add(en);
        more.DropDownItems.Add(lang);

        var layer = new ToolStripMenuItem(T("放到桌面层（被应用遮住）", "Pin to desktop (behind windows)"))
        { Checked = backLayer };
        layer.Click += (_, _) => SetLayer(!backLayer);
        more.DropDownItems.Add(layer);

        trayMenu.Items.Add(more);

        trayMenu.Items.Add(new ToolStripSeparator());
        trayMenu.Items.Add(MItem(T("退出", "Quit"), () => Application.Exit(), "Ctrl+Alt+Q"));

        trayMenu.ResumeLayout();
    }

    // 切语言：菜单、气泡、接入窗口、宠物页面一起换，不重启。
    static void SetLang(string lang)
    {
        CurLang = lang == "en" ? "en" : "zh";
        SaveLang(CurLang);
        try { if (tray != null) tray.Text = CurLang == "en" ? "PetHub · " + traySigTxt() : "PetHub · " + traySigTxt(); } catch { }
        try
        {
            var core = cc?.CoreWebView2;
            if (core != null)
                core.ExecuteScriptAsync("window.pethubSetLang && window.pethubSetLang('" + CurLang + "')");
        }
        catch { }
        // 接入窗口的文字是建窗口时拼死的，关掉再按新语言重开（本来开着才重开）
        bool reopen = aiWin != null && !aiWin.IsDisposed;
        try { if (reopen) aiWin.Close(); } catch { }
        if (reopen) { try { form.BeginInvoke(new Action(ShowAiConnect)); } catch { } }
        Balloon(T("语言已切换为中文", "Language: English"),
                T("菜单和提示都会跟着变。", "Menus and tips now speak English."),
                ToolTipIcon.Info);
        SayOnPage(T("好，说中文", "OK — English it is"));
    }

    static string traySigTxt() => trayTasks.Count == 0
        ? T("空闲", "idle")
        : T(trayTasks.Count + " 个任务", trayTasks.Count + " task(s)");

    static void ToggleTaskHud()
    {
        var core = cc?.CoreWebView2;
        if (core == null) return;
        try { core.ExecuteScriptAsync("String(window.pethubToggleHud ? window.pethubToggleHud() : 0)"); } catch { }
    }

    static void SayTask(string key)
    {
        var core = cc?.CoreWebView2;
        if (core == null) return;
        try
        {
            string js = "String(window.pethubSayTask ? window.pethubSayTask(" +
                        System.Text.Json.JsonSerializer.Serialize(key) + ") : 0)";
            core.ExecuteScriptAsync(js);
        }
        catch { }
    }

    // ---- 兜底轮询：页面 10 秒没上报（卡住/崩了/还在加载）才轮到它 ----
    static void PollTasksFallback()
    {
        if (tray == null || trayPolling) return;
        if (Environment.TickCount64 - lastPageReportTick < 10000) return;   // 页面还在活跃上报，别抢
        trayPolling = true;
        Task.Run(() =>
        {
            string json = null;
            try
            {
                using var c = new System.Net.Http.HttpClient { Timeout = TimeSpan.FromSeconds(2) };
                json = c.GetStringAsync("http://127.0.0.1:8765/api/state").GetAwaiter().GetResult();
            }
            catch { }
            try
            {
                if (form == null || form.IsDisposed) { trayPolling = false; return; }
                form.BeginInvoke(new Action(() =>
                {
                    try
                    {
                        // 桥不通 = 真的没任务在跑，别让托盘一直骗人
                        ApplyTaskJson(json ?? "{\"tasks\":[]}", false);
                    }
                    finally { trayPolling = false; }
                }));
            }
            catch { trayPolling = false; }
        });
    }
}

// ---------------- 单实例 ----------------
// 同一时间只允许一只桌宠：多开会变成多个全屏透明窗口 + 多个全局鼠标钩子，
// 互相抢点击，表现就是"点哪都没反应"。
static class SingleInstance
{
    static Mutex mutex;
    static EventWaitHandle poke;
    const string PokeName = @"Local\PetHubLight.Poke";

    // 已经有一只宠物在跑时，第二次启动就"戳它一下"（它会跳起来跟你说话），然后自己退出
    public static void PokeOther()
    {
        try { EventWaitHandle.OpenExisting(PokeName).Set(); } catch { }
    }

    const string QuitName = @"Local\PetHubLight.Quit";

    // 让已经在跑的那只自己退出（会摘掉托盘图标）。
    // 直接用 taskkill /F 会留下通知区的"幽灵图标"，所以安装器/卸载脚本先走这里。
    public static void RequestQuit()
    {
        try { EventWaitHandle.OpenExisting(QuitName).Set(); } catch { }
    }

    public static void WatchQuit(Action onQuit)
    {
        try
        {
            var q = new EventWaitHandle(false, EventResetMode.AutoReset, QuitName);
            var th = new Thread(() =>
            {
                while (true) { try { q.WaitOne(); } catch { return; } try { onQuit(); } catch { } }
            }) { IsBackground = true, Name = "pethub-quit" };
            th.Start();
        }
        catch { }
    }
    public static void WatchPoke(Action onPoke)
    {
        try
        {
            poke = new EventWaitHandle(false, EventResetMode.AutoReset, PokeName);
            var th = new Thread(() =>
            {
                while (true) { try { poke.WaitOne(); } catch { return; } try { onPoke(); } catch { } }
            }) { IsBackground = true, Name = "pethub-poke" };
            th.Start();
        }
        catch { }
    }

    public static bool Acquire()
    {
        try
        {
            mutex = new Mutex(true, @"Local\PetHubLight.SingleInstance", out bool created);
            if (created) return true;
            try
            {
                if (mutex.WaitOne(TimeSpan.FromMilliseconds(400))) return true;   // 前一个正在退出
            }
            catch { }
            return false;
        }
        catch
        {
            return true;   // 拿不到互斥体也不该拦着用户使用
        }
    }
}
