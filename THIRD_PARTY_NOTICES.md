# 第三方组件与资料

## Chaldea Laplace

- 上游：https://github.com/chaldea-center/chaldea
- 对应提交：`9970a57a7d150d5d551f583f6b2c4862a9f7e798`
- 许可证：AGPL-3.0，完整文本保留在`integrations/laplace/LICENSE`和`runtime/laplace/licenses/Chaldea-AGPL-3.0.txt`。
- 本地Dart适配入口位于`integrations/laplace/`。对应的Chaldea源码、适配器源码、构建脚本和依赖锁文件保留在`runtime/laplace/sources/`；发布包包含完整运行时及这些源码。
- 本地上游副本将Flutter补丁版本约束放宽至`>=3.47.4 <3.48.0`，并加入适配入口。构建方法见`integrations/laplace/README.md`。

## Flutter及运行时依赖

Flutter引擎与依赖通知保留在`runtime/laplace/licenses/`以及发布包`runtime/laplace/assets/NOTICES.Z`。Python发布依赖的许可证和通知保留在Windows包的`_internal/`中。

## Fate/Grand Automata

AP恢复流程参考FGA的局部识别和状态复核方式；没有集成其Android服务或Kotlin运行时。MIT许可证与引用提交保留在`data/licenses/FGA-LICENSE.txt`、`data/licenses/FGA-NOTICE.txt`。

## Atlas Academy与游戏资源

公开游戏数据由https://api.atlasacademy.io/提供，地图资源来自https://static.atlasacademy.io/。地图特征的准确来源与哈希见`data/navigation/halloween_2026_map.source.json`。从者、礼装、地图和游戏界面素材的权利属于各自权利人；本项目不将其声明为原创素材。

项目自有代码目前未另行指定统一开源许可证；第三方组件保留其原有许可证和署名。
