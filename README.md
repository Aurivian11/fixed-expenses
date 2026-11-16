# 固定支出 · Fixed Expenses

一个记录**每月固定开销**的私人小工具：房租、话费、会员订阅、保险……把每个月雷打不动的钱放在一起，看清它们什么时候扣、一个月一共要花多少。

- 后端只用 Python 标准库（`http.server`），无第三方依赖
- 前端是单个原生 HTML 文件，浅色玻璃质感界面，适配手机
- 数据保存在服务器上的 `data/expenses.json`

## 功能

| 功能 | 说明 |
|---|---|
| 月度轨道 | 把一个月画成表盘：指针是此刻，每笔扣款按扣款日落在轨道上（空心 = 待付、虚线 = 自动扣、绿勾 = 已付、红 = 过期未付），内圈是分类占比，点圆点跳到对应卡片 |
| 付款周期 | 月付 / 季付 / 年付；季付、年付按月均折算，只在真正扣款的月份计入当月应付 |
| 多币种 | 12 种货币（CNY、USD、EUR、HKD、GBP、JPY、SGD、AUD、CAD、KRW、TWD、CHF），所有汇总按实时汇率折算成人民币 |
| 自动扣款 / 自动续费 | 到扣款日自动算作已付；扣款失败可标记「未扣成功」 |
| 本月进度 | 本月应付、已付、剩余，以及下一笔扣款倒计时和过期提醒 |
| 实时消耗 | 按月均折算，显示「本月至今已花掉」的跳动金额，以及每天 / 每小时 / 每分钟的固定成本 |
| 试算取消 | 临时把某几项划掉，看每月、每年能省多少（不保存） |
| 导入 / 导出 | JSON 备份与恢复 |

## 数据安全

- **密码登录**：PBKDF2 哈希；会话是 HMAC 签名的 HttpOnly / Secure / SameSite=Strict Cookie，有效 30 天；同一 IP 15 分钟内输错 6 次即暂时锁定
- **防多设备互相覆盖**：每次保存都带版本号，服务器发现版本落后会拒绝（409），页面自动载入最新数据
- **不丢改动**：改动先存本机浏览器，再排队同步；断网、登录过期、关页面时都会保留，下次打开会询问是否用它覆盖服务器
- **每日快照**：每天第一次写入前把旧数据存到 `data/history/`，保留最近 30 天
- 安全响应头：CSP、`X-Frame-Options: DENY`、`nosniff`；页面 `Cache-Control: no-cache`，更新后不会看到旧版本

## 汇率

服务器从 [open.er-api.com](https://open.er-api.com)（备用 frankfurter.app）获取汇率，缓存 6 小时到 `data/rates.json`。拿不到时用上次缓存；完全没有缓存时前端使用内置估算值，并在页面上注明。

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
public/index.html                 前端：全部界面与逻辑
config/app.env.example            配置示例（真正的 app.env 不提交）
deploy/guding-zhichu.service      systemd 服务
deploy/nginx-zhichu-location.conf 反向代理 location 示例
data/                             运行时数据（不提交）
```
