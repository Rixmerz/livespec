const { helper } = require('./helpers');
const utils = require('./utils');

function main(x) {
  const y = helper(x);
  return utils.format(y);
}

module.exports = { main };
