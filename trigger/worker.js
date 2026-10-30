// تریگر پشتیبان رادار روی Cloudflare Workers
// هر ۱۵ دقیقه، با فاصله هفت دقیقه از تریگر اصلی، به گیت‌هاب می‌گوید اجرا کن.
// متغیر محیطی لازم: GH_TOKEN  (توکن fine-grained با دسترسی Actions: write)

const REPO = "amirchitaee/wp-radar";
const WORKFLOW = "radar-auto.yml";
const REF = "main";

async function dispatch(env) {
  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "radar-trigger",
    },
    body: JSON.stringify({ ref: REF }),
  });
  const text = await res.text();
  return { status: res.status, body: text.slice(0, 300) };
}

export default {
  // اجرای زمان‌بندی‌شده
  async scheduled(event, env, ctx) {
    const out = await dispatch(env);
    console.log("scheduled dispatch:", JSON.stringify(out));
  },

  // اجرای دستی با باز کردن آدرس ورکر در مرورگر
  async fetch(request, env) {
    const out = await dispatch(env);
    return new Response(JSON.stringify(out, null, 2), {
      status: out.status === 204 ? 200 : 500,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  },
};
