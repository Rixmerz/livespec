import { z } from "zod";
import * as zod from "zod";
import { makeLabel } from "./labels";

/** A postal address.
 * @schema: address
 */
export const Address = z.object({
  street: z.string(),
  zip: z.string().nullable(),
});

// The user as the API returns it.
export const User = z
  .object({
    id: z.string().uuid(),
    age: z.number().int().optional(),
    score: z.number(),
    big: z.bigint(),
    active: z.boolean().default(true),
    createdAt: z.string().datetime(),
    birthday: z.string().date(),
    seenAt: z.date(),
    nickname: z.string().nullish(),
    role: z.enum(["admin", "user"]),
    tags: z.array(z.string()),
    aliases: z.string().array(),
    home: Address,
    others: z.array(Address).optional(),
    meta: z.record(z.string(), z.unknown()),
    inline: z.object({ a: z.string() }),
    either: z.union([z.string(), z.number()]),
    count: z.coerce.number().int(),
  })
  .strict();

export const Admin = User.extend({
  level: z.number().int(),
  age: z.number().int(),
});

const Loose = zod.object({ label: zod.string().transform((s) => makeLabel(s)) });

const NotZod = other.object({ a: 1 });

export function useUser(raw: unknown) {
  return User.parse(raw);
}

export const Checked = z.object({ n: z.number() }).passthrough();
export { Loose };
