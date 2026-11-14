# Anime 知识库 · AstrBot 插件

基于 **Bangumi 归档** + **萌娘百科** 的部分番剧/角色知识库。给 LLM 提供 `search_anime`、`get_entity`、`get_translation`、`get_relations` 四个工具。

## 能回答什么

| 类型 | 例子 | 走哪层 | 评测 |
|---|---|---|---|
| 番剧名 | 「冰菓」「来自深渊」「星际牛仔」 | 名字索引 | 99.3% @1 |
| 角色名 | 「千反田爱瑠」「雷格」「战场原黑仪」 | 名字 / 别名索引 | 90.5% @1（4 字以上 98.3%） |
| 角色属性 | 「银发 红瞳」「黑长直」「身高 160 以上」 | 萌百属性 SQL | 100% 精确 / 0.26ms |
| 单集 | 「《来自深渊》第 1 集」 | 先定位作品，再 `get_entity(part="episodes")` | 99.3% @1 |
| 内容概要 | 「在末日世界旅行的少女」 | 语义 + 标签 | 93.8% @10 |
| 作品关系 | 「《冰菓》的制作公司」 | `get_relations` | — |
| 评分 | 「《冰菓》评分」→ 8.1 | `work_stats` | — |

**不擅长**具体情节回忆。
工具会明确回答「没有足够匹配的资料」，而不是编造。

## 安装
### 从 AstrBot 插件市场安装
### 或手动克隆到插件目录
```bash
cd /AstrBot/data/plugins
git clone https://github.com/ShioMI39/astrbot_plugin_animerag.git
```
数据包在webui里下载


**模型全部复用 AstrBot 的配置**，先在「服务提供商」页面建三类 provider：

| 类型 | 适配器 | 必填 | 用途                                                                            |
|---|---|---|-------------------------------------------------------------------------------|
| Embedding | OpenAI Embedding | **是** | 向量化（`api_base` 推荐使用 `https://api.siliconflow.cn/v1`，模型 `BAAI/bge-m3`，维度 1024） |
| Rerank | vLLM Rerank | 否 | 重排序（`api_base` 推荐使用 `https://api.siliconflow.cn`，后缀 `/v1/rerank`）             |
| chat_completion | DeepSeek 或任意 | 否 | 翻译部分番剧没有译文                                                                    |

然后把 provider ID 填进插件配置的「模型配置」组。

## 命令

| 命令 | 作用 |
|---|---|
| `/检查更新` | 比对 Bangumi 存档版本，提示有没有新 dump |
| `/召回测试 <查询>` | 打印检索层对这个查询的命中（属性层 / 语义层的分数） |

建议在 WebUI 使用查看。

## 配置

`_conf_schema.json` 里 9 项，常用的：

| 项 | 默认 | 说明                                                  |
|---|---|-----------------------------------------------------|
| `data_dir` | 留空 | 数据目录。留空用插件目录下的 `data/`                              |
| `embedding_provider_id` | 留空 | **必填**。AstrBot 的 Embedding provider ID，数据包默认使用1024维 |
| `rerank_provider_id` | 留空 | 选填。填了才启用重排序功能                                       |
| `translate_provider_id` | 留空 | 选填。翻译用的 chat_completion provider ID                 |
| `top_k` | 8 | 每次给 LLM 的上下文条数                                      |
| `min_score` | 0.55 | 余弦阈值，低于此值明确回答「没有资料」                                 |

三个 `*_provider_id` 都直接复用 AstrBot「服务提供商」里配好的模型。

## 数据规模

```
data/anime.db            332 MB   角色 94,994 / 作品 25,368 / 单集 334,630
data/moegirl.db           24 MB   萌百页面 12,797 / 属性 103,565     ← 可选
data/moegirl_bodies.json  38 MB   7,255 个角色的正文                ← 可选
data/translate.db         30 MB   译文 52,250 条
data/vec/                191,485 块 / 989 MB
```
