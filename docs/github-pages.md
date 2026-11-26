# GitHub Pages 发布

公开地址：https://cavin2012.github.io/stock_indices/

仓库 Settings → Pages → Build and deployment 的 Source 选择 GitHub Actions。推送 main 后，Deploy GitHub Pages 工作流只发布 _site 中五个白名单文件。不会将整个仓库或本机运行目录打包。

首次发布及日常更新：

1. 在本机运行 python update_index.py，确认采集和证据覆盖情况。
2. 运行 python build_public_demo.py，从本机最新结果生成 data.example.js。
3. 运行 python scripts/build_pages.py，检查输出；该步骤只打包静态文件，不采集、不编译。
4. 提交脱敏的 data.example.js 并推送 main，等待 Pages 工作流成功。

公开页面展示最后一次发布的实测聚合快照，不自动采集。GitHub Pages 不能执行 Python。论坛原文、作者键、帖子编号、缓存、档案和本机 data.js 均不上传；公开 data.js 由脱敏快照二次清理后生成。

保留上游 MIT LICENSE 和原作者署名。原仓库作为 upstream，新仓库作为 origin。
