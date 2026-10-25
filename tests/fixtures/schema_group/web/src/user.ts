import { z } from "zod";

/** @schema: user */
export interface User {
  id: string;
  email: string;
  fullName: string;
}

/** @schema: user */
export const UserSchema = z.object({
  id: z.string(),
  email: z.string(),
  fullName: z.string(),
});
