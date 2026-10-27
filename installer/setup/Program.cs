// PetHub 桌宠 · 安装程序
// 单文件 exe：载荷（payload.zip）编进程序里，支持自选安装路径、桌面/开始菜单快捷方式、开机自启。
// 也支持静默安装：PetHub-Setup.exe --silent --dir="D:\Apps\PetHub" [--autostart]
using Microsoft.Win32;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Net.Http;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

static class Setup
{
    public const string AppName = "PetHub 桌宠";
    public const string ExeName = "PetHubLight.exe";
    public const string AppVersion = "1.0";
    public const string Publisher = "PetHub Contributors";
    public const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    // 「已安装的应用」（设置 → 应用 → 已安装的应用）里的条目；用户级，不需要管理员
    public const string ArpKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\PetHub";

    public static string DefaultDir => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PetHub");

    [STAThread]
    static void Main(string[] args)
    {
        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        bool silent = false, autostart = false;
        string dir = null;
        foreach (var a in args)
        {
            if (a.Equals("--silent", StringComparison.OrdinalIgnoreCase)) silent = true;
            else if (a.Equals("--autostart", StringComparison.OrdinalIgnoreCase)) autostart = true;
            else if (a.StartsWith("--dir=", StringComparison.OrdinalIgnoreCase))
                dir = a.Substring("--dir=".Length).Trim('"');
        }

        if (silent)
        {
            try { Install(dir ?? DefaultDir, true, autostart, null); Environment.Exit(0); }
            catch (Exception ex) { Console.Error.WriteLine(ex.Message); Environment.Exit(1); }
        }
        Application.Run(new SetupForm(dir ?? DefaultDir));
    }

    // ---------------- 核心：解包 + 快捷方式 + 开机自启 ----------------
    public static void Install(string dir, bool desktopShortcut, bool autostart,
                               Action<int, string> progress)
    {
        KillRunning();
        Directory.CreateDirectory(dir);
        LastWarnings.Clear();

        using (var s = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
        {
            if (s == null) throw new InvalidOperationException("安装包损坏：缺少载荷 payload.zip");
            using (var zip = new ZipArchive(s, ZipArchiveMode.Read))
            {
                int total = zip.Entries.Count, i = 0;
                foreach (var e in zip.Entries)
                {
                    i++;
                    string rel = e.FullName.Replace('/', Path.DirectorySeparatorChar);
                    string dst = Path.GetFullPath(Path.Combine(dir, rel));
                    if (!dst.StartsWith(Path.GetFullPath(dir), StringComparison.OrdinalIgnoreCase))
                        throw new IOException("安装包包含非法路径：" + e.FullName);
                    if (e.FullName.EndsWith("/")) { Directory.CreateDirectory(dst); continue; }
                    Directory.CreateDirectory(Path.GetDirectoryName(dst)!);
                    e.ExtractToFile(dst, true);
                    progress?.Invoke(i * 88 / Math.Max(1, total), "正在释放文件 " + i + "/" + total);
                }
            }
        }

        // 环境自检：缺什么就地补齐，别让用户装完打不开
        progress?.Invoke(90, "正在检查运行环境…");
        EnsurePython(dir, progress);
        EnsureWebView2(progress);

        string exe = Path.Combine(dir, ExeName);
        progress?.Invoke(96, "正在创建快捷方式…");
        if (desktopShortcut) CreateShortcut(Path.Combine(Environment.GetFolderPath(
            Environment.SpecialFolder.DesktopDirectory), AppName + ".lnk"), exe, dir);
        CreateShortcut(Path.Combine(Environment.GetFolderPath(
            Environment.SpecialFolder.Programs), AppName + ".lnk"), exe, dir);

        progress?.Invoke(98, autostart ? "正在设置开机自启…" : "正在收尾…");
        SetAutostart(autostart, exe);
        WriteUninstaller(dir, exe);
        RegisterArp(dir, exe);
        progress?.Invoke(100, "安装完成");
    }

    // ================= 运行环境自检 =================
    public static readonly List<string> LastWarnings = new List<string>();

    const string Wv2Clsid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}";
    const string Wv2Url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703";
    const string PyUrl = "https://www.python.org/ftp/python/3.13.7/python-3.13.7-embed-amd64.zip";

    // WebView2 是桌宠的显示内核，Win10 1803+ 大多自带（随 Edge），但精简版/服务器版可能没有。
    static bool HasWebView2()
    {
        foreach (var hive in new[] { Registry.LocalMachine, Registry.CurrentUser })
            foreach (var sub in new[] { @"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\",
                                        @"SOFTWARE\Microsoft\EdgeUpdate\Clients\" })
                try
                {
                    using var k = hive.OpenSubKey(sub + Wv2Clsid);
                    if (k?.GetValue("pv") is string pv && pv.Length > 2 && pv != "0.0.0.0")
                        return true;
                }
                catch { }
        // 有些精简安装只落目录不写注册表
        try
        {
            var d = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                 @"Microsoft\EdgeWebView\Application");
            if (Directory.Exists(d) && Directory.GetDirectories(d).Length > 0) return true;
        }
        catch { }
        return false;
    }

    // 自带的 Python（载荷里带一份 embeddable）。有它就不挑目标机器装没装 Python。
    static void EnsurePython(string dir, Action<int, string> progress)
    {
        string py = Path.Combine(dir, "runtime", "python", "python.exe");
        if (File.Exists(py)) { progress?.Invoke(92, "运行环境：自带 Python 已就绪"); return; }

        progress?.Invoke(92, "缺少 Python 运行时，正在下载补齐…");
        string tmp = Path.Combine(Path.GetTempPath(), "pethub-python.zip");
        try
        {
            Download(PyUrl, tmp, p => progress?.Invoke(92, "下载 Python 运行时 " + p + "%"));
            progress?.Invoke(94, "正在展开 Python 运行时…");
            var target = Path.Combine(dir, "runtime", "python");
            Directory.CreateDirectory(target);
            ZipFile.ExtractToDirectory(tmp, target, true);
        }
        catch (Exception ex) { LastWarnings.Add("Python 运行时补齐失败：" + ex.Message); }
        finally { try { File.Delete(tmp); } catch { } }

        if (File.Exists(py)) { progress?.Invoke(94, "运行环境：Python 已补齐"); return; }
        LastWarnings.Add("没有可用的 Python 运行时：桌宠界面能显示，但任务同步（桥接 / 接入应用）不会工作。");
    }

    static void EnsureWebView2(Action<int, string> progress)
    {
        if (HasWebView2()) { progress?.Invoke(95, "运行环境：WebView2 已就绪"); return; }

        progress?.Invoke(95, "缺少 WebView2 运行时，正在下载…");
        string tmp = Path.Combine(Path.GetTempPath(), "pethub-webview2.exe");
        try
        {
            Download(Wv2Url, tmp, p => progress?.Invoke(95, "下载 WebView2 运行时 " + p + "%"));
            progress?.Invoke(96, "正在安装 WebView2（约 1 分钟，别关窗口）…");
            using var pr = Process.Start(new ProcessStartInfo(tmp, "/silent /install")
            { UseShellExecute = false, CreateNoWindow = true });
            pr?.WaitForExit(300000);
            for (int i = 0; i < 20 && !HasWebView2(); i++) Thread.Sleep(1000);
        }
        catch (Exception ex) { LastWarnings.Add("WebView2 安装失败：" + ex.Message); }
        finally { try { File.Delete(tmp); } catch { } }

        if (HasWebView2()) { progress?.Invoke(96, "运行环境：WebView2 已安装"); return; }
        LastWarnings.Add("缺少 WebView2 运行时，桌宠会白屏/不显示。联网后重跑安装包即可，" +
                         "或手动安装：https://go.microsoft.com/fwlink/p/?LinkId=2124703");
    }

    static void Download(string url, string dest, Action<int> onPercent)
    {
        using var hc = new HttpClient { Timeout = TimeSpan.FromMinutes(10) };
        // 有些站点会拦空 UA
        try { hc.DefaultRequestHeaders.UserAgent.ParseAdd("PetHub-Setup/1.1"); } catch { }
        using var resp = hc.GetAsync(url, HttpCompletionOption.ResponseHeadersRead)
                           .GetAwaiter().GetResult();
        resp.EnsureSuccessStatusCode();
        long total = resp.Content.Headers.ContentLength ?? -1;
        using var src = resp.Content.ReadAsStreamAsync().GetAwaiter().GetResult();
        using var fs = File.Create(dest);
        var buf = new byte[81920];
        long done = 0;
        int n, last = -1;
        while ((n = src.Read(buf, 0, buf.Length)) > 0)
        {
            fs.Write(buf, 0, n);
            done += n;
            if (total > 0)
            {
                int pct = (int)(done * 100 / total);
                if (pct != last) { last = pct; onPercent?.Invoke(pct); }
            }
        }
    }

    public static void KillRunning()
    {
        // 先请它自己退：--quit 会让桌宠摘掉托盘图标再退出。
        // 硬杀（taskkill /F）会在通知区留下点不动的"幽灵图标"，那是用户看到的
        // "托盘里好几个圆点、一会儿又变一个"的来历。
        Process[] procs;
        try { procs = Process.GetProcessesByName("PetHubLight"); } catch { procs = new Process[0]; }
        if (procs.Length == 0) return;

        string exe = null;
        foreach (var p in procs)
        {
            try { exe = p.MainModule?.FileName; } catch { }
            if (!string.IsNullOrEmpty(exe)) break;
        }
        if (!string.IsNullOrEmpty(exe))
        {
            try
            {
                using var q = Process.Start(new ProcessStartInfo(exe, "--quit")
                { UseShellExecute = false, CreateNoWindow = true });
                q?.WaitForExit(3000);
            }
            catch { }
            for (int i = 0; i < 12; i++)
            {
                try { if (Process.GetProcessesByName("PetHubLight").Length == 0) return; } catch { return; }
                Thread.Sleep(500);
            }
        }
        // 还不走就只能硬来
        try
        {
            foreach (var p in Process.GetProcessesByName("PetHubLight"))
            {
                try { p.Kill(); p.WaitForExit(2000); } catch { }
            }
        }
        catch { }
    }

    public static void CreateShortcut(string lnkPath, string target, string workDir)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(lnkPath)!);
            Type t = Type.GetTypeFromProgID("WScript.Shell");
            if (t == null) return;                       // 极端精简系统没有 WSH，宁可没快捷方式也不报错
            dynamic shell = Activator.CreateInstance(t);
            dynamic lnk = shell.CreateShortcut(lnkPath);
            lnk.TargetPath = target;
            lnk.WorkingDirectory = workDir;
            lnk.IconLocation = target + ",0";
            lnk.Description = "PetHub 桌面宠物";
            lnk.Save();
        }
        catch { }
    }

    public static void SetAutostart(bool on, string exe)
    {
        try
        {
            using var k = Registry.CurrentUser.CreateSubKey(RunKey, true);
            if (on) k?.SetValue("PetHub", "\"" + exe + "\"");
            else k?.DeleteValue("PetHub", false);
        }
        catch { }
    }

    // 登记到 Windows「已安装的应用」，这样能在系统设置里看到版本、大小并标准卸载
    static void RegisterArp(string dir, string exe)
    {
        try
        {
            long bytes = 0;
            try
            {
                foreach (var f in Directory.EnumerateFiles(dir, "*", SearchOption.AllDirectories))
                {
                    try { bytes += new FileInfo(f).Length; } catch { }
                }
            }
            catch { }

            string un = Path.Combine(dir, "卸载 PetHub.cmd");
            using var k = Registry.CurrentUser.CreateSubKey(ArpKey, true);
            k.SetValue("DisplayName", AppName);
            k.SetValue("DisplayVersion", AppVersion);
            k.SetValue("Publisher", Publisher);
            k.SetValue("InstallLocation", dir);
            k.SetValue("UninstallString", "\"" + un + "\"");
            k.SetValue("DisplayIcon", "\"" + exe + "\"");
            k.SetValue("EstimatedSize", (int)Math.Min(int.MaxValue, bytes / 1024), RegistryValueKind.DWord);
            k.SetValue("NoModify", 1, RegistryValueKind.DWord);
            k.SetValue("NoRepair", 1, RegistryValueKind.DWord);
            k.SetValue("HelpLink", "https://github.com/");
        }
        catch { }   // 写不进注册表不影响安装本身
    }

    static void WriteUninstaller(string dir, string exe)
    {
        string un = Path.Combine(dir, "卸载 PetHub.cmd");
        string body =
            "@echo off\r\nchcp 65001 >nul\r\n" +
            "echo 正在关闭 PetHub…\r\n" +
            "if exist \"%~dp0" + ExeName + "\" \"%~dp0" + ExeName + "\" --quit\r\n" +
            "timeout /t 3 >nul\r\n" +
            "taskkill /IM PetHubLight.exe >nul 2>nul\r\n" +
            "timeout /t 2 >nul\r\n" +
            "taskkill /IM PetHubLight.exe /F >nul 2>nul\r\n" +
            "reg delete \"HKCU\\" + @"Software\Microsoft\Windows\CurrentVersion\Run" + "\" /v PetHub /f >nul 2>nul\r\n" +
            "reg delete \"HKCU\\" + @"Software\Microsoft\Windows\CurrentVersion\Uninstall\PetHub" + "\" /f >nul 2>nul\r\n" +
            "del \"%USERPROFILE%\\Desktop\\" + AppName + ".lnk\" >nul 2>nul\r\n" +
            "del \"%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\" + AppName + ".lnk\" >nul 2>nul\r\n" +
            "echo 正在删除程序目录（宠物存档保留在 %LOCALAPPDATA%\\PetHub\\settings.json）…\r\n" +
            "start \"\" cmd /c \"timeout /t 2 >nul & rmdir /s /q \\\"%~dp0\\\"\"\r\n";
        try { File.WriteAllText(un, body, System.Text.Encoding.UTF8); } catch { }
    }
}

class SetupForm : Form
{
    readonly string initDir;
    readonly TextBox pathBox = new TextBox();
    readonly CheckBox cbDesktop = new CheckBox();
    readonly CheckBox cbAuto = new CheckBox();
    readonly Button btnInstall = new Button();
    readonly Button btnClose = new Button();
    readonly ProgressBar bar = new ProgressBar();
    readonly Label status = new Label();

    public SetupForm(string dir)
    {
        initDir = dir;
        Text = "安装 " + Setup.AppName;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        StartPosition = FormStartPosition.CenterScreen;
        MaximizeBox = false; MinimizeBox = false;
        ClientSize = new Size(560, 292);
        BackColor = Color.FromArgb(248, 250, 253);
        Font = new Font("Microsoft YaHei UI", 9f);

        var title = new Label
        {
            Text = "🐾  PetHub 桌面宠物",
            Font = new Font("Microsoft YaHei UI", 15f, FontStyle.Bold),
            ForeColor = Color.FromArgb(28, 40, 58),
            Location = new Point(24, 20), AutoSize = true
        };
        var sub = new Label
        {
            Text = "一只住在桌面上的小宠物，会把各个 AI 工具的工作进度讲给你听。",
            ForeColor = Color.FromArgb(96, 112, 136),
            Location = new Point(26, 58), AutoSize = true
        };

        var lblPath = new Label { Text = "安装到：", Location = new Point(26, 96), AutoSize = true };
        pathBox.Text = initDir;
        pathBox.Location = new Point(96, 93);
        pathBox.Width = 340;
        var btnBrowse = new Button { Text = "浏览…", Location = new Point(444, 92), Size = new Size(78, 26) };
        btnBrowse.Click += (_, _) =>
        {
            using var d = new FolderBrowserDialog { Description = "选择 PetHub 的安装目录", ShowNewFolderButton = true };
            try { d.SelectedPath = pathBox.Text; } catch { }
            if (d.ShowDialog(this) == DialogResult.OK) pathBox.Text = d.SelectedPath;
        };

        cbDesktop.Text = "创建桌面快捷方式";
        cbDesktop.Checked = true;
        cbDesktop.Location = new Point(96, 128);
        cbDesktop.AutoSize = true;

        cbAuto.Text = "开机自动启动（随 Windows 一起跑）";
        cbAuto.Location = new Point(96, 152);
        cbAuto.AutoSize = true;

        var note = new Label
        {
            Text = "安装完成后运行桌面上的「" + Setup.AppName + "」即可（无需另装 Python）。\n" +
                   "缺 WebView2 / Python 运行时会自动下载补齐，需要联网。\n" +
                   "救命热键：Ctrl+Alt+Q 退出   Ctrl+Alt+H 收起/唤出",
            ForeColor = Color.FromArgb(120, 134, 156),
            Location = new Point(26, 176), AutoSize = true
        };

        bar.Location = new Point(96, 226);
        bar.Size = new Size(340, 8);
        bar.Style = ProgressBarStyle.Continuous;
        status.Location = new Point(96, 238);
        status.AutoSize = true;
        status.ForeColor = Color.FromArgb(96, 112, 136);

        btnInstall.Text = "开始安装";
        btnInstall.Location = new Point(348, 244);
        btnInstall.Size = new Size(96, 32);
        btnInstall.BackColor = Color.FromArgb(74, 144, 226);
        btnInstall.ForeColor = Color.White;
        btnInstall.FlatStyle = FlatStyle.Flat;
        btnInstall.FlatAppearance.BorderSize = 0;
        btnInstall.Click += (_, _) => DoInstall();

        btnClose.Text = "退出";
        btnClose.Location = new Point(452, 244);
        btnClose.Size = new Size(76, 32);
        btnClose.Click += (_, _) => Close();

        Controls.AddRange(new Control[] { title, sub, lblPath, pathBox, btnBrowse,
            cbDesktop, cbAuto, note, bar, status, btnInstall, btnClose });
        AcceptButton = btnInstall;
    }

    async void DoInstall()
    {
        string dir = pathBox.Text.Trim().Trim('"');
        if (dir.Length < 3 || !Path.IsPathRooted(dir))
        {
            MessageBox.Show(this, "请选择一个有效的安装路径。", "路径不对",
                MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }
        btnInstall.Enabled = btnClose.Enabled = false;
        bar.Value = 0;
        try
        {
            var prog = new Progress<(int, string)>(p =>
            {
                bar.Value = Math.Min(100, p.Item1);
                status.Text = p.Item2;
            });
            await Task.Run(() => Setup.Install(dir, cbDesktop.Checked, cbAuto.Checked,
                (v, s) => ((IProgress<(int, string)>)prog).Report((v, s))));
            status.Text = "安装完成";
            if (Setup.LastWarnings.Count > 0)
            {
                MessageBox.Show(this,
                    "安装完成，但有几点要注意：\n\n· " + string.Join("\n· ", Setup.LastWarnings.ToArray()),
                    "装好了（有提示）", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
            var r = MessageBox.Show(this,
                "安装完成！现在就启动 " + Setup.AppName + " 吗？",
                "完成", MessageBoxButtons.YesNo, MessageBoxIcon.Information);
            if (r == DialogResult.Yes)
            {
                try
                {
                    Process.Start(new ProcessStartInfo(Path.Combine(dir, Setup.ExeName))
                    { WorkingDirectory = dir, UseShellExecute = true });
                }
                catch (Exception ex) { MessageBox.Show("启动失败：" + ex.Message); }
            }
            Close();
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, "安装失败：\n" + ex.Message, "出错了",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
            btnInstall.Enabled = btnClose.Enabled = true;
        }
    }
}
