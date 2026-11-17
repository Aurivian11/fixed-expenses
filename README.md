# 固定支出 · Fixed Expenses

一个记录**每月固定开销**的私人小工具：房租、话费、会员订阅、保险……把每个月雷打不动的钱放在一起，看清它们什么时候扣、一个月一共要花多少。

- 后端只用 Python 标准库（`http.server`），无第三方依赖
- 前端是单个原生 HTML 文件，浅色玻璃质感界面，适配手机
- 数据保存在服务器上的 `data/expenses.json`

<p align="center">
  <img src="docs/screenshot.jpg" alt="固定支出 · 桌面端首页" width="74%">
  &nbsp;
  <img src="docs/screenshot-mobile.jpg" alt="固定支出 · 手机端首页" width="22%">
</p>

<p align="center"><sub>截图为演示数据</sub></p>

## 功能

| 功能 | 说明 |
|---|---|
| 月度轨道 | 把一个月画成表盘：指针是此刻，每笔扣款按扣款日落在轨道上（空心 = 待付、虚线 = 自动扣、绿勾 = 已付、红 = 过期未付），内圈是分类占比，点圆点跳到对应卡片 |
| 现金流时间线 | 过去月份的实付记录 + 本月 + 未来 12 个月预测，标出最重的月份（年付、季付扣款月）和月均线；点柱子查看当月明细，过去的月份也能补标已付 |
| 扣款提醒 | 每天定时检查，通过 **Telegram / Bark** 推送：手动付款提前 N 天、扣款当天、过期未付；自动续费提前提醒「不需要记得取消」；年付季付提前 7 天；每月 1 号发本月汇总 |
| 自动扣款 / 自动续费 | 到扣款日自动算作已付；扣款失败可标记「未扣成功」 |
| 试用 / 到期 | 免费试用到期前不计费并提前 3 天提醒；合约到期后不再计入并提前 7 天提醒 |
| 暂停 | 暂时不扣的项目（比如健身卡冻结）可以暂停，暂停期间不计入任何金额，恢复后接着算 |
| 涨价记录 | 改金额时自动记一笔价格历史，卡片上显示「↑ 涨 ¥14/月 · 10月起」，过去的月份按当时价格计算 |
| 多币种 + 手续费 | 12 种货币按实时汇率折算人民币，可设置信用卡外币手续费（如 1.5%） |
| 支付方式汇总 | 按分类或按支付方式（每张卡、支付宝……）查看每月被扣多少，方便和账单核对 |
| 收入占比 | 填上月收入，显示固定支出占几成、每月还剩多少可自由支配 |
| 本月进度 / 下一笔 | 本月应付、已付、剩余，下一笔扣款倒计时 |
| 实时消耗 | 「本月至今已花掉」的跳动金额，以及每天 / 每小时 / 每分钟的固定成本 |
| 试算取消 | 临时把某几项划掉，看每月、每年能省多少（不保存） |
| 导入 / 导出 | JSON 备份与恢复 |

## 扣款提醒

提醒由后端的 `notify.py` 负责：服务启动后每分钟检查一次，到了设置里的「每天检查时间」（默认 09:00）就把当天需要提醒的内容合并成一条消息发出；同一件事只提醒一次，发送失败 15 分钟后重试。

在页面的「设置」里可以**直接填写自己的 Telegram 机器人（Bot Token + Chat ID，可一键自动获取 Chat ID）和 Bark（服务器地址 + 设备 Key）**，保存前会先验证 Token 是否有效、机器人能否给这个聊天发消息；还可以开关渠道、调整提前天数、查看「今天的提醒预览」并发送测试推送。

推送凭据单独保存在服务器的 `data/notify-config.json`（权限 600），不会出现在账单数据、导出文件或备份里，页面上只显示末尾 4 位。`config/app.env` 里的 `GZ_TG_*` / `GZ_BARK_*` 只作为页面没设置时的默认值。

## 部署

```bash
# 1. 代码放在 /root/固定支出
mkdir -p data && chmod 700 data

# 2. 配置
cp config/app.env.example config/app.env && chmod 600 config/app.env
python3 server.py --hash                                   # 输入密码，把输出填到 GZ_PASSWORD_HASH
python3 -c "import secrets;print(secrets.token_hex(32))"   # 填到 GZ_SECRET

# 3. 启动（监听 Docker 网桥 172.18.0.1:8130，供反向代理访问）
cp deploy/guding-zhichu.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now guding-zhichu
```

反向代理示例见 `deploy/nginx-zhichu-location.conf`（挂在 `/zhichu/` 子路径；前端全部使用相对路径，任意子路径都可用）。

改密码：`python3 server.py --hash`，替换 `config/app.env` 里的 `GZ_PASSWORD_HASH` 后 `systemctl restart guding-zhichu`。

## 目录

```
server.py                         后端：页面、登录、数据读写（带版本号）、每日快照、汇率缓存
notify.py                         扣款提醒：规则计算、定时检查、Telegram / Bark 推送
public/index.html                 前端：全部界面与逻辑
config/app.env.example            配置示例（真正的 app.env 不提交）
deploy/guding-zhichu.service      systemd 服务
deploy/nginx-zhichu-location.conf 反向代理 location 示例
docs/                             README 截图
data/                             运行时数据（不提交）
```
