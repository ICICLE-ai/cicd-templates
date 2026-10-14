const fs = require('node:fs');
const childProcess = require('node:child_process');
const event = process.argv[2];
const source = fs.readFileSync('source-value.txt', 'utf8').trim();
if (event === 'prepare') fs.writeFileSync('prepared.txt', source);
if (event === 'postinstall') {
  const legacyPeerDeps = childProcess.execFileSync('npm', ['config', 'get', 'legacy-peer-deps'], {encoding: 'utf8'}).trim();
  if (legacyPeerDeps !== 'true') throw new Error('project .npmrc was not honored');
  fs.writeFileSync('production-mode.txt', process.env.NODE_ENV || 'development');
}
if (event === 'build') {
  const dependency = require('runtime-dependency');
  if (dependency() !== 'dependency lifecycle ran') throw new Error('dependency lifecycle did not run');
  fs.mkdirSync('dist', {recursive: true});
  fs.writeFileSync('dist/index.html', source);
}
