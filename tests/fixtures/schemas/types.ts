// Fixture for TypeScript schema extraction: interfaces and object type aliases.

export enum Status {
  Active = "active",
  Banned = "banned",
}

type Role = "admin" | "user";

type Id = string;

/** Fields every record carries. */
interface Base {
  id: number;
  createdAt: Date;
}

/**
 * A user as the frontend sees it.
 * @schema: user-profile
 */
export interface User extends Base, Auditable<string> {
  nickname?: string;
  deletedAt: string | null;
  email: string | undefined;
  "display-name": string;
  status: Status;
  role: Role;
  tags: string[];
  friends: Array<User>;
  meta: Record<string, unknown>;
  address: Address;
  score: bigint;
  active: boolean;
  either: string | number;
  pair: [string, number];
  callback: () => void;
  [extra: string]: unknown;
  greet(): string;
}

export interface Admin extends User {
  level: number;
  id: number;
}

export type Address = {
  street: string;
  zip?: string | null;
};

type Point = Base & { x: number };

export function makeUser(id: Id): User {
  return { id } as User;
}
