module.exports = () => require('node:child_process').execFileSync(require('node:path').join(__dirname, 'native-check'), {encoding: 'utf8'}).trim();
