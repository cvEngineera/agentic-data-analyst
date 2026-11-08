"""本地 Web 演示的静态资源包（v0.3 新增）。

只包含 index.html 一个自包含页面：无外部 CDN、无构建步骤，
Python 标准库 http.server 直接读出来发给浏览器即可。

页面既能离线打开（展示版本演进与评估数字快照），
也能连上 scripts/serve.py 后实时提问、跑评估、看 git 代码变化。
"""