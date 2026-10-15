const http = require("node:http");
const server = http.createServer((request, response) => {
  let body = "";
  request.on("data", chunk => { body += chunk; });
  request.on("end", () => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      status: "ok", version: process.env.BUILD_VERSION || require("./package.json").version,
      build_sha: process.env.BUILD_SHA, uid: process.getuid(),
      path: request.url, method: request.method, body,
    }));
  });
});
// Keep the legacy default; the image tests also supply the new pod's PORT=5000.
server.listen(Number(process.env.PORT || "8000"), "0.0.0.0");
process.on("SIGTERM", () => server.close(() => process.exit(0)));
