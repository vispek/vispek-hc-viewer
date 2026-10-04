# vispek-hc-viewer（中文说明）

Vispek HC1500 多光谱相机的本机网页查看器：实时画面、LED 控制、扫描、白板参考、单波段图、
指数图和光谱，都在一个只对本机开放的页面里。它建立在 [`vispek-hc`](https://github.com/vispek/vispek-hc-camera)
SDK 之上，记录下来的就是普通的采集文件夹，`vispek-hc` 命令行同样能读。

英文版见 [README.md](README.md)；两者有出入时以英文版为准。

> **紫外线。** HC1500 的 1、2 号灯发出 UV-C，3、4 号灯为 UV-A。UV-C 会伤害眼睛和皮肤，
> 而且看不见。除非启动时加 `--allow-uv`，查看器不会点亮 1–4 号灯；加之前请先读 `vispek-hc`
> 的 `SAFETY.md`。程序被强制结束（不是 Ctrl-C）时灯不会灭：请把设备的**每一根线**
> （包括相机线）都拔掉确认。

## 状态

预发布（0.1）。在一台真机（macOS，Apple 芯片）上试过：实时画面、LED 控制、LED 5–17 扫描、
用白纸做的白板参考及其校正、图像和光谱。Linux：已实现，未真机验证。Windows：不支持，因为
SDK 在 Windows 上还不能锁定相机参数。

## 启动

```bash
python3 -m venv hc-env && source hc-env/bin/activate
pip install vispek-hc-viewer        # 会同时安装 vispek-hc SDK
vispek-hc setup                     # macOS：编译相机控制工具，只需一次
vispek-hc-viewer --simulate         # 不接硬件：仿真设备
vispek-hc-viewer                    # 真机
```

程序会打印一个形如 `http://127.0.0.1:8642/#token=...` 的地址并打开它。`#` 后面的部分是
这个页面的钥匙，拿到它的人就能操作设备，不要贴到别处。在终端按 Ctrl-C 结束查看器，
所有 LED 会被关掉。

真机需要的条件与 `vispek-hc` 相同：`PATH` 里有 `ffmpeg`；启动查看器的那个终端有相机权限
（macOS 第一次会询问）；macOS 上要有 `vispek-hc setup` 编译出的相机控制工具。运行
`vispek-hc check` 可以看到缺什么。

| 选项 | |
|---|---|
| `--data-dir DIR` | 采集、校正包和导出放在哪里（默认 `./vispek-hc-data`） |
| `--port N` | 127.0.0.1 上的端口（默认 8642；0 表示任选一个空闲端口） |
| `--simulate` | 只打开仿真设备，不碰硬件 |
| `--allow-uv` | 允许点亮 1–4 号灯 |
| `--no-browser` | 不自动打开页面 |
| `-v` | 记录每个请求 |

## 页面

- **Device（左）**：连接和断开；选择串口和相机；"Check" 让 6 号灯闪一下，报告相机是否
  看到、延迟多少、黑电平是多少；"Tune" 用于新设备，需要白板在视野里。
- **Illumination（左）**：17 颗 LED。开关按旁边的 PWM 点亮一颗灯，再点另一颗时前一颗
  自动关。"S" 把这个 PWM 存为扫描用的值。1–4 号灯是锁着的，除非启动时加了 `--allow-uv`。
- **Camera（左）**：曝光和增益。改了以后，之前录的白板参考就不能用于新的扫描，因为校正包
  只适用于相同设置下的扫描。
- **Live（中）**：相机画面，每秒约 10 帧。"Show clipping" 把到 255 的像素标成品红。拖一个
  矩形可以读它的均值；右侧有直方图、削顶比例和一个清晰度数字（调焦用）。
- **录制（左下）**："Scan" 每颗选中的灯录一帧，外加一帧暗帧；"White reference" 对着白板
  做同样的事，并生成校正包；"Frame" 和 "Dark" 存单张画面。"Channels & options" 里选灯、平均次数，
  以及写进采集的说明。
- **Captures（下）**：数据文件夹里的全部采集，点一下打开。
- **一个采集（中和右）**：彩色合成（RGB、CIR、UV）、单波段（左右方向键切换）、NDVI、
  NDWI、主成分和均值，带色标。选一个校正包就能看相对白板的值；不选时画面标着 RAW，比较
  波段的视图是关着的，因为在原始数据上它们只反映各灯的驱动功率。校正后的图默认按固定的
  0–1 范围显示（白就是白，两次采集可以直接比）；"Auto" 按各自的 1%–99% 拉伸，"Manual"
  用你给的上下限。点击得到一个像素的光谱，拖动得到一个区域的光谱（均值和标准差），最多
  保留八个，可存为 CSV。"Export" 把数据写成 ENVI、TIFF 或 `cube.npz`。

鼠标：滚轮缩放，右键拖动（或按住 Alt 拖动）平移，双击适应窗口。

## 第一次使用

1. Connect。按 "Check"：应显示 paired，黑电平 5, 5, 5（在暗箱里）。
2. 把白板放进视野，按 "White reference"。会出现一个同名的校正包。
3. 把样品放进视野，按 "Scan"。
4. 扫描打开时已套用校正包。在图上点击、拖动看光谱。

## 数据文件夹

```
vispek-hc-data/
├── captures/<名字>/        采集文件夹（格式见 vispek-hc 的 docs/data-format.md）
├── calibrations/<名字>/    校正包
├── exports/<名字>/         "Export" 生成的 ENVI、TIFF、cube.npz
└── .cache/thumbnails/      列表用的小图，可以随时删
```

用 `vispek-hc record` 录的采集文件夹，拷进 `captures/` 再按 "Refresh" 就能看到。查看器
从不删除或覆盖任何采集、校正包和导出。

## 它为设备和电脑的安全做了什么

- 只监听 127.0.0.1，没有任何选项能改。
- 每个请求都要带程序打印的地址里的 token。
- 1–4 号灯由服务端拒绝，不只是页面上隐藏，除非启动时加了 `--allow-uv`。
- 扫描进行时，别的操作不能开关 LED。
- 手动点亮的灯，30 秒内没有任何页面联系就会自动关（页面被关掉、电脑睡眠等）。
- 结束程序时关掉所有 LED；如果无法确认，会明确说出来。

详见 [docs/security.md](docs/security.md)。

## 限制

- 一台设备；同一时间只有一个页面在驱动它，第二个页面看到的是同一个状态。
- 画面和数值是相机处理之后的 8 位码值，不是辐亮度；"相对白板"是一个指数，只在设置完全
  相同的扫描之间可比（见 `vispek-hc` 的 `docs/calibration.md`）。
- 视频采集会列出来，但不能显示。

## 开发

```bash
uv sync --group dev
uv run pytest            # 不需要硬件，不联网
uv run ruff format --check . && uv run ruff check . && uv run mypy
```

改代码前请读 [AGENTS.md](AGENTS.md)；全部接口见 [docs/http-api.md](docs/http-api.md)。

## 许可

Apache-2.0，见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。
