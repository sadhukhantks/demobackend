"""Embed data.json into template.html -> index.html (served by the Next prototype route)."""
import os, json
here = os.path.dirname(os.path.abspath(__file__))
tpl = open(os.path.join(here, "template.html"), encoding="utf-8").read()
data = open(os.path.join(here, "data.json"), encoding="utf-8").read()
# JSON inside a <script> block must not contain a closing tag or line separators
data = data.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
assert tpl.count("/*__DATA__*/") == 1
out = tpl.replace("/*__DATA__*/", data)
open(os.path.join(here, "index.html"), "w", encoding="utf-8").write(out)
print("index.html", os.path.getsize(os.path.join(here, "index.html")) // 1024, "KB")
