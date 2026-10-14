const fs = require("node:fs");
fs.mkdirSync("dist/assets", { recursive: true });
fs.writeFileSync("dist/index.html", "<!doctype html><title>Image smoke fixture</title>");
fs.writeFileSync("dist/assets/app.1234abcd.js", "window.smoke = true;\n");
