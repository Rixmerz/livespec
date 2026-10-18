import { define } from "../utils.ts";

/**
 * Render's health check.
 * @spec:platform-health
 */
export const handler = define.handlers({
  /**
   * 200 when the store answers.
   * @spec:platform-health-get
   */
  async GET() {
    await getSettings();
    return new Response("ok");
  },
  POST: handle(async (ctx) => {
    return json(await createThing(ctx));
  }),
});

/**
 * The page.
 * @spec:platform-health-page
 */
export default define.page<typeof handler>(function Health({ data }) {
  return renderHealth(data);
});
