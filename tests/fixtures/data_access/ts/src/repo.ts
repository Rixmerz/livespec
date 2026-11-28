import knex from "knex";
import { PrismaClient } from "@prisma/client";

const prisma = new PrismaClient();
const db = knex({ client: "pg" });

export async function loadUser(id: string) {
  return prisma.user.findUnique({ where: { id } });
}

export async function saveUser(email: string) {
  return prisma.user.create({ data: { email } });
}

export async function ordersFor(id: number) {
  return db("orders").where({ user_id: id }).select("*");
}

export async function addLine(row: object) {
  return db.insert(row).into("order_lines");
}

export async function dropOld() {
  await knex("sessions").where("expired", true).del();
}

export async function rawSql(userId: number, status: string) {
  return db.raw(`
    SELECT o.id, p.name
    FROM orders o
    JOIN products p ON p.id = o.product_id
    WHERE o.user_id = ${userId} AND o.status = '${status}'
  `);
}

export async function insertPayment(amount: number) {
  return db.raw("INSERT INTO payments (amount) VALUES (?)", [amount]);
}

export function message() {
  return "Please select a file from the list, then update the record";
}
