// Fresh 2 shapes: functions bound to `const`, framework callbacks wrapped in
// a call, and a registry of object literals whose methods share one name.
import { define } from "./utils.ts";

/**
 * Plain arrow bound to a const.
 * @spec:arrow-plain
 */
const formatName = (first: string, last: string) => {
  return join(first, last);
};

/**
 * Exported arrow.
 * @spec:arrow-exported
 */
export const greet = async (name: string) => {
  return formatName(name, "");
};

/**
 * Loads the signed-in person.
 * @spec:auth-session
 */
export const session = define.middleware(async (ctx) => {
  await loadUser(ctx);
  return ctx.next();
});

/**
 * Security headers.
 * @spec:platform-headers
 */
const securityHeaders = define.middleware(async (ctx) => {
  const res = await ctx.next();
  setHeaders(res);
  return res;
});

/** A value, not a function: no symbol. */
export const LIMIT = compute(3);

export const TOOLS = [
  {
    name: "assign_ticket",
    async run(args) {
      return assign(args);
    },
  },
  {
    name: "get_capacity",
    run(args) {
      return capacity(args);
    },
  },
  {
    run() {
      return anonymous();
    },
  },
];

export const app = define.app([session, securityHeaders]);
