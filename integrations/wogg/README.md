# 玩偶哥哥 T4 桥接

独立运行的桥接服务。在飞牛影视原生搜索中提供玩偶哥哥目录结果，读取夸克分享的分集列表，并在播放器请求视频时将所选文件转存到当前夸克账号。详情有夸克返回的源文件尺寸时，会显示宽高。最终播放画质由实际返回的流决定。

## 运行条件

- 飞牛 NAS 上安装并启用 [fnmedia-ext 官方完整 FPK](https://github.com/yujian0815/fnmedia-ext/releases)，并在扩展管理页的「网盘与小雅」登录夸克。桥接按 v1.1.0 完整包中的夸克接口适配，升级扩展后请重新验证。
- 桥接复用当前登录状态和扩展的网盘播放接口。公开仓库提供 T4 适配，公开源码构建的精简版缺少桥接需要的 `pan` 模块和网盘播放接口，无法打开夸克分集或播放。桥接源码使用已安装的完整包运行。
- Python 3.11 或更新版本。桥接进程需要读取扩展的本地夸克登录状态、访问扩展接管的影视 Unix Socket，并写入自己的状态目录。按实际设备调整运行账号和安装路径。

## 配置与启动

复制 `bridge.env.example` 为本地 `bridge.env` 并填写配置。`WOGG_ORIGIN` 必填，填站点的 HTTPS 域名地址。支持末尾斜杠及标准 443 端口。自行选择和配置站点，仅播放自己有权访问的内容。`WOGG_POSTER_HOSTS` 可选，填写逗号分隔的 HTTPS 图片域名，只允许同站或这里列出的域名作为海报地址。站点变更后重启桥接。

可参考 `wogg-fnmedia-bridge.service.example` 配置 systemd。按安装位置调整运行账号、工作目录、环境文件、Python 路径和状态目录，复用完整 FPK 的 Python 环境。也可以从桥接源码目录前台运行，以下命令要求已填写 `bridge.env`。

```sh
umask 077
set -a
. ./bridge.env
set +a
/var/apps/fnmedia-ext/target/repo/.venv/bin/python -m uvicorn bridge:app \
  --host 127.0.0.1 --port "$WOGG_T4_PORT" --no-access-log
```

`curl http://127.0.0.1:18724/health` 确认桥接服务已启动。在扩展管理页打开「订阅与 JS 源」，粘贴以下 JSON 并选择「解析并导入」。如修改了端口，同时更新 `api`。在飞牛影视里搜索片名，选择标记为「玩偶哥哥 · 夸克」的结果，然后打开分集测试播放。

```json
{
  "sites": [
    {"key": "wogg-local-quark", "name": "玩偶哥哥", "type": 4,
     "api": "http://127.0.0.1:18724/t4", "searchable": 1}
  ]
}
```

保持桥接只监听 NAS 的回环地址。`/health` 用来确认桥接进程状态。遇到网页验证、分享失效或夸克接口调整时，目录或播放可能暂时不可用。

电脑版网页如果遇到飞牛网关丢掉媒体端口并报「抱歉，播放出错了」，可使用 [完整版 v1.1.0 的网页 HLS 兼容补丁](patches/v1.1.0-web-hls-origin.patch)。在扩展的 `repo` 目录针对该文件先执行 `patch --dry-run -p1` 检查。检查通过后备份 `proxy/app.py`，用 `patch -p1` 应用，并在飞牛应用中心重启在线扩展。使用其他版本时，先确认是否已包含修复。

首次播放可能需要等候转存。已转存的单集保存在夸克的 `fnmedia转存` 目录，会占用网盘容量。登录状态由扩展管理，桥接仅在进程内使用凭据。桥接 SQLite 缓存包含分享链接、提取码和文件标识，请保存在本地私有目录。停用时可删掉导入的 T4 来源并停止桥接服务。已转存的文件可在夸克网盘内自行管理。

## 离线测试

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -v
```

测试使用模拟站点、夸克和飞牛接口，覆盖站点配置验证、按集转存、账号切换、长 HLS 清单及分片保留时间。
