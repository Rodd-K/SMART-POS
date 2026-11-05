"""Adds the Export feature to server.py (3 small edits). Safe to run once; it backs up server.py first."""
import shutil, sys

SRC = "server.py"
s = open(SRC, encoding="utf-8").read()
if "import exporter" in s:
    sys.exit("server.py already has the export feature. Nothing changed.")

A1 = "from urllib.parse import urlparse\n"
A2 = "import mpesa\n"
A3 = "            self.reply(*api(c, m, p, u, body))\n"
for a in (A1, A2, A3):
    if s.count(a) != 1:
        sys.exit("Could not find this line in server.py exactly once, so nothing was changed:\n" + a)

NEW = '''            if p.startswith("/api/export/") and m == "GET":  # admin-only data export
                if u["role"] != "admin":
                    return self.reply(403, {"error": "Admin access required"})
                res = exporter.run(c, p.rsplit("/", 1)[1], parse_qs(urlparse(self.path).query))
                if not res:
                    return self.reply(400, {"error": "Unknown export or invalid dates"})
                fname, data, ctype, n = res
                audit(c, u, "export", f"{fname} ({n} rows)")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
'''
shutil.copy(SRC, "server_before_export.py")
s = s.replace(A1, "from urllib.parse import urlparse, parse_qs\n").replace(A2, "import mpesa\nimport exporter\n").replace(A3, NEW + A3)
open(SRC, "w", encoding="utf-8").write(s)
print("Done. Backup saved as server_before_export.py. Restart the server.")
