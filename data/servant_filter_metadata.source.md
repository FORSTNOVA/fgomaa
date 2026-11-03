# 从者筛选元数据

来源：[Atlas Academy CN basic_servant 静态导出](https://api.atlasacademy.io/export/CN/basic_servant.json)。
接口说明：[Atlas Academy API 源码中的静态导出说明](https://github.com/atlasacademy/fgo-game-data-api/blob/master/app/main.py)。

2026-11-01 下载，保留 458 个从者的 id、name、className、rarity；以 id 为键保存至 servant_filter_metadata.json。
运行时只读取该本地缓存，不上传账号信息，也不需要联网查询每个目标。
筛选使用真实职阶/稀有度，不根据角色等级推测星级；缺少可靠元数据时先清除旧条件，再执行全量列表查找。
