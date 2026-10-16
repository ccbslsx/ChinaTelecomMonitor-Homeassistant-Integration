# CTM电信 Home Assistant 集成（本地分支 · 含修复）

> **本仓库是 [`hlhk2017/ChinaTelecomMonitor-Homeassistant-Integration`](https://github.com/hlhk2017/ChinaTelecomMonitor-Homeassistant-Integration) 的 fork，版本 `1.1.8`。**
> 在 v1.1.7 基础上修了两个 bug，其余功能与上游一致。上游若有更新，可按需合并。

## 本分支相对 v1.1.7 的改动

### 1. 修复冷启动时多账号必有一个 setup 失败

**现象**：HA 冷启动后，配置了两个及以上号码时，必有一个 config entry 报
`RuntimeError: Added route will never be executed, method GET is already registered`
（注册卡片静态路由 `/china_telecom/ctm-telecom-card.js`），该号码全部传感器不可用；
手动 reload 一次即恢复正常。

**原因**：HA 会**并发**加载同一集成下的多个 config entry。原实现为「查 flag → await → 置 flag」，
两个 entry 同时通过检查，随后都去注册同一 URL，第二个抛错。

**修复**：模块级 `asyncio.Lock` 串行化卡片引导；**在首个 `await` 之前就置 flag**（失败回滚）；
静态路由注册包 `try/except RuntimeError`；并将整段卡片注册与传感器链路隔离
（**卡片永远不会拖垮传感器**）。同时修复 `async_unload_entry` 使用了 HA 2025.6 已移除的
`async_forward_entry_unload` 的问题（原写法一卸载即 AttributeError）。

### 2. 修复流量总量 / 剩余用量不准

**现象**：部分套餐（如「达量降速」类）上报的通用流量总量为 0 或等于已用量、剩余恒为 0。

**原因**：`qryImportantData` 返回的 `totalAmount` / `commonFlow` 只是**首页文案提示**
（`showField`=1 时给已用、=2 时给剩余，`total`/`balance` 常为 `"0"`）。
真实的总量只存在于 `userFluxPackage`（逐流量包明细）与 `flowList`（逐行）中，
而原 `sensor.py` 从未调用 `userFluxPackage`。

**修复**：`to_summary()` 优先采用 `userFluxPackage` + `flowList` 计算通用/专用额度，
`totalAmount` 仅作兜底；新增 `parse_traffic_kb()` 支持 `1.07GB` / `0KB` / `/共10.00GB` /
`达40GB降速` 等形态。新增 4 个传感器：**通用流量剩余**、**通用流量使用率**、
**流量包明细**（逐包已用/总量/剩余/失效时间，并带 `source` 属性标明数据来源）、**套餐名称**。

**效果**（实测）：「天翼畅享40GB（199元）套餐」类账号由 `总量26GB / 剩余0GB / 100%`
修正为 `总量101.07GB / 剩余75.07GB / 25.72%`（40GB 主套餐 + 7 个行业流量包）。

> 原有传感器 entity_id 全部保留，可直接原地升级，无需重建配置。

---

# CTM电信 Home Assistant 集成（一体化）

这是一个基于 [ChinaTelecomMonitor](https://github.com/Cp0204/ChinaTelecomMonitor) 核心逻辑，并**直接集成到 Home Assistant** 的中国电信话费、通话、流量套餐用量监控解决方案。

**本项目已将数据获取功能与 Home Assistant 集成合二为一，不再需要单独部署 `ChinaTelecomMonitor` 的 API 服务。**

**参考了[ChinaTelecomOperators/ChinaTelecom](https://github.com/ChinaTelecomOperators/ChinaTelecom)中DeviceId的处理方式。**
**本次修复后，需要更新集成，获取到 `DeviceId` 后，填入本集成的“短信授权 DeviceId”配置项，再重新加载集成。即可恢复使用**
## DeviceId的获取：
### 网站1：https://commissions-yields-exception-personally.trycloudflare.com

### 网站2：https://telecom.nufe.ccwu.cc
### 先打开网站短信登录授权设备，复制设备id，配置到集成输入框

## 特性

* **一体化部署：** 无需额外的 Docker API 服务，直接在 Home Assistant 中配置和运行。
* **实时数据：** 监控话费余额、本月消费、流量用量（通用/专用）、通话时长等关键数据。
* **流量与通话使用率：** 新增流量已用百分比和通话已用百分比传感器，更直观地了解使用情况。
* **自动处理登录与 Token：** 集成内部处理电信账号的登录、Token 刷新和过期重试机制。
* **Home Assistant 原生体验：** 作为 HACS 集成或自定义组件安装，提供友好的配置界面。

## 效果展示

UI配置框：

<img width="618" height="579" alt="image" src="https://github.com/user-attachments/assets/b4162ae0-b46b-4732-ba58-448338d74554" />



实际效果：
![7809c0446e278c6a4f354c6b33c4b80](https://github.com/user-attachments/assets/0bac6024-cf65-494d-b9e7-03247dcd9ec2)


## 如何使用本集成

### ✅ 方法一：通过 HACS 安装（推荐）

1.  打开 Home Assistant 左侧菜单，点击 **HACS**。
2.  进入右上角菜单 → 选择 **“自定义存储库”**。
3.  填入仓库地址：`https://github.com/hlhk2017/ChinaTelecomMonitor-Homeassistant-Integration`
4.  类型选择 **集成 (Integration)**，点击添加。
5.  返回 HACS 主界面，搜索并安装 **“CTM电信”**。
6.  安装完成后，前往 **“设置 → 设备与服务 → 添加集成”**。
7.  搜索 **CTM电信**，点击添加并完成配置（只需输入手机号和密码）。

[![快速通过 HACS 链接安装](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=hlhk2017&repository=ChinaTelecomMonitor-Homeassistant-Integration&category=integration)

### 方法二：手动安装

1.  下载本项目源代码。
2.  将整个 `custom_components/china_telecom` 目录复制到 Home Assistant 的配置路径下：
    `[你的 Home Assistant 配置目录]/custom_components/china_telecom`
3.  **重启 Home Assistant。** (重要：不是重新加载集成，是完整重启 Home Assistant 服务)
4.  前往 **“设置 → 设备与服务 → 添加集成”**。
5.  搜索 **CTM电信**，点击添加并完成配置。

### 卡片配置

<img width="941" height="751" alt="image" src="https://github.com/user-attachments/assets/a1cfc708-0168-4741-afc2-2964765c4eee" />

新增一个卡片，配置集成后自动注册，添加卡片搜ctm即可，只需要选择对应的设备。

## 感谢

本项目核心数据获取逻辑大量参考了 [Cp0204/ChinaTelecomMonitor](https://github.com/Cp0204/ChinaTelecomMonitor) 项目的代码，在此表示衷心感谢！
