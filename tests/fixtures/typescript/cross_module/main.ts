import { helper } from './helpers';
import * as utils from './utils';

export function main(x: number): string {
  const y = helper(x);
  return utils.format(y);
}
