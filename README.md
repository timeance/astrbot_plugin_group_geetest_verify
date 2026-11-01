# Group-Geetest-Verify
一个AstrBot的入群验证码插件，使用极验Geetest V4验证，有效防止机器人入群。仅支持aiocqhttp

![Group-Geetest-Verify](https://socialify.git.ci/VanillaNahida/astrbot_plugin_group_geetest_verify/image?description=1&font=KoHo&forks=1&issues=1&language=1&name=1&owner=1&pattern=Circuit%20Board&pulls=1&stargazers=1&theme=Auto)

<div align="center">
  
  [![GitHub license](https://img.shields.io/github/license/VanillaNahida/astrbot_plugin_group_geetest_verify?style=flat-square)](https://github.com/VanillaNahida/astrbot_plugin_group_geetest_verify/blob/main/LICENSE)
  [![GitHub stars](https://img.shields.io/github/stars/VanillaNahida/astrbot_plugin_group_geetest_verify?style=flat-square)](https://github.com/VanillaNahida/astrbot_plugin_group_geetest_verify/stargazers)
  [![GitHub forks](https://img.shields.io/github/forks/VanillaNahida/astrbot_plugin_group_geetest_verify?style=flat-square)](https://github.com/VanillaNahida/astrbot_plugin_group_geetest_verify/network)
  [![GitHub issues](https://img.shields.io/github/issues/VanillaNahida/astrbot_plugin_group_geetest_verify?style=flat-square)](https://github.com/VanillaNahida/astrbot_plugin_group_geetest_verify/issues)
  [![python3](https://img.shields.io/badge/Python-3.12+-blue.svg?style=flat-square)](https://www.python.org/)
  [![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-brightgreen.svg?style=flat-square)]()
  
</div>

<p align="center">
  <img src="logo.png" alt="Group-Geetest-Verify Logo">
</p>

# 效果展示

<p align="center">
  <img src="img/1.jpg" width="300">
</p>

# 使用方法

## 升级到 v1.3.8

升级前备份插件目录、配置和数据目录中的 `verify_states.db`。重新加载会在原 SQLite 表上增加截止时间、机器人身份、轮次和重试字段，不重建数据库。运行基线为 AstrBot 4.24.2 以上、Python 3.12 以上；依赖为 `aiohttp>=3.14.5,<4` 和 `aiosqlite>=0.22.1,<1`。

验证开始后按绝对截止时间运行；重载、补发提示和错误回答不延长期限。重启会恢复身份与期限完整的待验证任务，只连接记录中的 AstrBot 平台实例和原机器人。原机器人离线或身份不匹配时保留记录并定期重连，不切换其他机器人执行操作。身份完整的待验证记录也只由原机器人处理回答和退群通知。

踢出首次失败后追加 5、15、30 秒三次重试；只有确认踢出成功或成员已离群才清理记录。重试耗尽保留待验证状态，尽力发一次管理提示；成员仍可完成验证，管理员可用 `/重新验证 @用户` 创建新轮次。通过、离群或重新验证后，旧轮次失效。

历史记录缺少身份或截止时间时，启动日志会按群汇总人数，**不会因升级自动踢人**。请管理员核查后使用现有重新验证命令；不要删除数据库来消除告警。验证过程消息撤回仍只处理本次运行已记录的消息 ID，不会扫描历史聊天。极验接口失败仍回退算术题。

后端请使用 [timeance/group-verify-service](https://github.com/timeance/group-verify-service) 的安全修复与依赖升级 PR，插件填入后台创建的普通 Key；最早的默认 Key 用于管理后台。正式部署、真实群聊测试与密钥轮换应另行安排。

回滚时恢复上一版插件代码并保留数据库；新增字段可保留，避免丢失验证数据。旧版代码没有任务恢复和重试保护，回滚期间需管理员人工核查待验证成员。

## v1.3.7 历史变更

v1.3.7 的配置和数据库可保留，v1.3.8 会执行上述增量迁移。

此版本同步上游至 [`ef3d9d8`（v1.3.4）](https://github.com/VanillaNahida/astrbot_plugin_group_geetest_verify/commit/ef3d9d8d0fbfa9a159edfeab043d46ad78ce3a14)，并保留本分支的多机器人路由及 v1.3.6 修复。

此版本移植了[上游 #14](https://github.com/VanillaNahida/astrbot_plugin_group_geetest_verify/issues/14) 的修复，解决正确验证码被判错、验证链接参数丢失和重试时数学题为空的问题，并保留指定验证机器人功能。

如果开启极验后仍收到数学题，请检查全局 API 地址、API 密钥，以及目标群的 `enable_geetest_verify`。群级设置优先于全局设置，已有群单独关闭极验时不会被升级自动开启；生成极验链接失败时仍会回退到算术验证。

  > [!WARNING]
  >
  > 请使用 AstrBot ≥ `v4.24.2` 与 Python ≥ `3.12`。

## 方案一：本地部署后端服务

### 前置条件
1. 一台有公网IP的服务器
2. 一个不要钱的域名，用来托管验证后端服务
3. 安装了PHP 8.4的服务器，可使用[宝塔面板](https://www.bt.cn/)按照文档安装

### 部署后端
请查看 [timeance 后端安装与安全升级说明](https://github.com/timeance/group-verify-service)。

### 插件安装与配置
1. 在插件市场搜索插件 `astrbot_plugin_group_geetest_verify` 或 `入群网页验证插件`
2. 安装插件（可以在插件市场安装，或者复制仓库地址，在WebUI中粘贴地址安装）
3. 配置插件
    1. 登录极验Geetest官网，注册账号并创建一个新的Geetest V4项目
    2. 按照[文档](https://github.com/VanillaNahida/astrbot_plugin_group_geetest_verify#%E5%AE%89%E8%A3%85%E6%AD%A5%E9%AA%A4)部署验证后端服务，按照文档配置好Geetest的ID和Key并启动。
    3. 在插件配置中填写验证后端服务的相关信息，包括`验证后端地址`、`API Key`等；填写后端管理后台创建的普通 Key
    4. 配置入群验证的相关参数，如验证超时时间、验证失败后的操作等。
4. 保存重载插件配置，使插件生效。

## 方案二：使用开发者部署好的服务

如果您没有自己的公网服务器和域名，也可以使用开发者部署好的服务。

### 获取后端地址和API Key
请加群 [195260107](https://qm.qq.com/q/1od5TMYrKE) 获取验证后端的 URL 和 API Key。

**在群内发送“/申请密钥”命令，bot会自动发送密钥到你的QQ邮箱**

若需要自定义接收密钥的邮箱，请使用这个命令：“/申请密钥 example@example.com”，将 `example@example.com` 替换为你自己的邮箱
如果签发失败，请直接私聊我，说明来意即可，看到了就会回复。

### 插件配置
1. 在插件市场搜索插件 `astrbot_plugin_group_geetest_verify` 或 `入群网页验证插件`
2. 安装插件
3. 配置插件
    1. 在WebUI中填写你获得的验证后端地址和API Key
    2. 配置入群验证的相关参数，如验证超时时间、验证失败后的操作等。
4. 保存重载插件配置，使插件生效。

  > [!TIP]
  >
  > 安装并配置好插件后建议**重启bot**，确保插件生效。

## 同一群有多个 QQ 机器人

在 WebUI 的每群配置中填写 `负责验证的 AstrBot Bot ID`，即可让同一群中只有指定的 AstrBot 适配器实例处理入群事件、验证回复和插件命令。

- 填写 `/sid` 显示的 `Bot ID`，也就是 AstrBot 事件的 `platform_id`，例如 `八千代` 或 `zar`。
- 不要填写 `/sid` 的 `UID`；它是执行命令的用户 ID。即使 Bot ID 与 QQ 昵称相同，插件比较的也是 AstrBot 配置 ID，而不是昵称。
- 指定机器人仍须在目标群中拥有管理员或群主权限。指定模式下如果插件无法确认其群角色，会跳过验证，不会继续执行。
- 留空时保持旧版行为：收到事件的 QQ 机器人只有在角色为管理员或群主时处理；权限查询失败时沿用兼容模式。

配置示例：

```json
{
  "group_id": "123456789",
  "enabled": true,
  "verification_platform_id": "八千代"
}
```

# 验证过程消息撤回

在插件设置页面的全局默认配置或对应群配置中，开启 **验证结束后撤回过程消息**（`recall_verification_messages`）。默认关闭，仅支持 QQ/OneBot；群配置可单独覆盖全局设置。

验证成功、超时踢出、错误次数达到上限、管理员绕过、强制重新验证或成员离群时，清理这一轮机器人发送的等级提示、链接或数学题、错误提醒、最后一分钟提醒及踢出预告。错误后刷新链接仍属于同一轮，直到验证结束才统一清理。

保留验证成功通知及欢迎图片、等级达标免验证通知、绕过通知、实际踢出后的最终通知，以及管理员命令回执。用户自己发送的验证码和聊天消息不受此开关影响；已有的 `recall_unverified_messages` 继续独立控制未验证用户的非验证码发言。

仅记录启用后本次插件运行期间发送的消息 ID，不扫描群历史，异常重启前的记录不恢复；已记录的轮次会继续完成清理。平台拒绝撤回或未返回消息 ID 时会记录日志，不影响验证和踢人操作。踢人失败时保留待验证状态，不发送“已被请出”通知。

QQ 实机验收可使用测试群：分别完成一次验证成功和一次超时踢出，确认过程提示消失、最终结果通知和用户验证码保留；再关闭开关重复验证，确认过程消息保留。

# 命令总览

| 命令 | 示例用法 | 权限要求 | 说明 |
|------|------|----------|------|
| `/重新验证` | `/重新验证 @用户` | 群主/管理员/Bot管理员 | 强制指定用户重新验证。需要@需要重新验证的用户 |
| `/绕过验证` | `/绕过验证 @用户` | 群主/管理员/Bot管理员 | 让指定用户绕过验证，该用户入群时将不再需要验证 |
| `/开启验证` | `/开启验证` | 群主/管理员/Bot管理员 | 开启当前群的入群验证功能 |
| `/关闭验证` | `/关闭验证` | 群主/管理员/Bot管理员 | 关闭当前群的入群验证功能 |
| `/查看验证配置` | `/查看验证配置` | 群主/管理员/Bot管理员 | 查看当前群的验证配置信息 |
| `/设置验证超时时间` | `/设置验证超时时间 秒数` | 群主/管理员/Bot管理员 | 设置当前群的验证超时时间（秒），超时未验证的用户将被踢出群 |
| `/开启等级验证` | `/开启等级验证` | 群主/管理员/Bot管理员 | 开启当前群的等级验证功能，QQ等级达到最低等级的用户将自动跳过验证 |
| `/关闭等级验证` | `/关闭等级验证` | 群主/管理员/Bot管理员 | 关闭当前群的等级验证功能 |
| `/设置最低验证等级` | `/设置最低验证等级 等级数` | 群主/管理员/Bot管理员 | 设置当前群的最低验证等级（0-64），QQ等级大于等于此等级的用户将自动跳过验证 |
| `/设置踢出延迟` | `/设置踢出延迟 3` | 群主/管理员/Bot管理员 | 设置当前群的踢出延迟时间（秒），用户验证失败后将在延迟时间后被踢出群 |

# 致谢
- [@yjwmidc](https://github.com/yjwmidc) 验证后端贡献者
- [@makotowu](https://github.com/makotowu) 负责验证后端的部署和前后端重构工作，为本项目提供公益验证服务
- [astrbot_plugin_Group-Verification_PRO](https://github.com/huntuo146/astrbot_plugin_Group-Verification_PRO) 参考该代码实现的入群验证插件，感谢该项目作者。

# bug反馈
如果在使用过程中遇到任何问题，欢迎你通过以下方式反馈：
- [Issue](https://github.com/VanillaNahida/Group-Geetest-Verify/issues)
- QQ群：[195260107](https://qm.qq.com/q/1od5TMYrKE) （处理最快，预计一周内解决）

# QQ群：
 - 一群：621457510
 - 二群：1031065631
 - 三群：195260107 （推荐）
 - 四群：1074471035

# Star History

[![Star History Chart](https://api.star-history.com/svg?repos=VanillaNahida/Group-Geetest-Verify&type=Date)](https://star-history.com/#VanillaNahida/Group-Geetest-Verify&Date)
