#!/usr/bin/env node
// stdin (1行JSON): {"method","path","token","host","body"|"fileBase64"+"fileName"+"fileType"}
//
// 常にPod自身のloopback(127.0.0.1:8080)へ接続し、Hostヘッダをそのインスタンスの
// ZITADEL_EXTERNALDOMAIN値(k3d: zitadel.zitadel.svc.cluster.local、本番:
// idp.aramakisai.com)へ明示的に差し替える。ZitadelはTCP接続先ではなくHostヘッダの
// 値からインスタンスを解決するため(cloudflared等のリバースプロキシがoriginへ転送する際に
// 元のHostを保持するのと同じ仕組み)、この差し替えなしでは"Instance not found"になる。
// fetch()(undici)はHostヘッダの上書きをFetch仕様上のforbidden headerとして拒否するため、
// 上書きが可能な低レベルAPIであるNode組み込みのhttpモジュールを使う。
const http = require("http");

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.on("data", (c) => (data += c));
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

(async () => {
  const req = JSON.parse(await readStdin());
  const headers = {
    Authorization: `Bearer ${req.token}`,
    Host: req.host,
  };

  let body;
  if (req.fileBase64) {
    const boundary = "----zitadelapi" + Date.now();
    const fileBuf = Buffer.from(req.fileBase64, "base64");
    const pre = Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${req.fileName}"\r\nContent-Type: ${req.fileType || "application/octet-stream"}\r\n\r\n`,
    );
    const post = Buffer.from(`\r\n--${boundary}--\r\n`);
    body = Buffer.concat([pre, fileBuf, post]);
    headers["Content-Type"] = `multipart/form-data; boundary=${boundary}`;
    headers["Content-Length"] = body.length;
  } else if (req.body !== undefined && req.body !== null) {
    body = Buffer.from(JSON.stringify(req.body));
    headers["Content-Type"] = "application/json";
    headers["Content-Length"] = body.length;
  }

  const httpReq = http.request(
    {
      hostname: "127.0.0.1",
      port: 8080,
      path: req.path,
      method: req.method,
      headers,
    },
    (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => {
        let parsed;
        try {
          parsed = JSON.parse(data);
        } catch {
          parsed = data;
        }
        process.stdout.write(JSON.stringify({ status: res.statusCode, body: parsed }));
      });
    },
  );
  httpReq.on("error", (e) => {
    process.stdout.write(JSON.stringify({ status: 0, error: String(e) }));
  });
  if (body) httpReq.write(body);
  httpReq.end();
})();
