# weixin transport vendor

这里是微信 claw 插件私有的 Node vendor 目录，只放插件自己的桥接和 transport 代码。

当前边界：

1. 只复用微信 transport 能力，不把 OpenClaw 的 channel/runtime/messaging 耦合层搬进宿主或插件桥接层。
2. Python 插件通过 `bridge.mjs` 调用这里的 Node transport，统一走结构化 JSON 请求和错误返回。
3. 扫码登录返回给宿主的二维码预览必须是可直接渲染的图片资源；如果上游只给扫码落地页 URL，插件要先在本地转成图片 data URL，再交给宿主通用 `artifacts/preview_artifacts` 渲染。

别在这里继续堆宿主特判。这里的职责只有一件事：把微信 transport 包成第三方插件自己的实现。
