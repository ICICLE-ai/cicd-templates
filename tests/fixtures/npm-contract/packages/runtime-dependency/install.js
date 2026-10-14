const fs = require('node:fs');
require('node:child_process').execFileSync('cc', ['native.c', '-o', 'native-check']);
fs.writeFileSync('installed.txt', 'dependency lifecycle ran');
