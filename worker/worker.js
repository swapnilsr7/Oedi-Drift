/**
 * Urban Thinking — Field Archive relay.
 *
 * Deploy this on Cloudflare Workers (free tier). It's the only piece of this
 * project that runs "live" — everything else is either static files or a
 * scheduled GitHub Action. Its one job: take an uploaded image from your
 * site, ask Claude to describe/tag it, and hand the tags back. Your
 * Anthropic API key lives here as a Worker secret, never in the site's code.
 *
 * Setup (see README.md for full walkthrough):
 *   1. workers.cloudflare.com -> Create Worker -> paste this file in.
 *   2. Settings -> Variables -> add secret ANTHROPIC_API_KEY.
 *   3. Settings -> Variables -> add ALLOWED_ORIGIN = your GitHub Pages URL
 *      (e.g. https://yourname.github.io) so only your site can call this.
 *   4. Copy the Worker's URL into WORKER_URL in index.html.
 */

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const allowed = env.ALLOWED_ORIGIN || "*";

    const corsHeaders = {
      "Access-Control-Allow-Origin": allowed,
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405, headers: corsHeaders });
    }

    if (allowed !== "*" && origin !== allowed) {
      return new Response("Origin not allowed", { status: 403, headers: corsHeaders });
    }

    try {
      const { imageBase64, mediaType } = await request.json();
      if (!imageBase64 || !mediaType) {
        return new Response(JSON.stringify({ error: "imageBase64 and mediaType required" }), {
          status: 400,
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }

      const anthropicRes = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-api-key": env.ANTHROPIC_API_KEY,
          "anthropic-version": "2023-06-01",
        },
        body: JSON.stringify({
          model: "claude-sonnet-4-6",
          max_tokens: 300,
          messages: [
            {
              role: "user",
              content: [
                { type: "image", source: { type: "base64", media_type: mediaType, data: imageBase64 } },
                {
                  type: "text",
                  text:
                    "Describe this design/architecture/fashion/art image for a similarity-search system. " +
                    "Respond ONLY with JSON, no preamble, no markdown fences: " +
                    '{"tags": ["4 to 7 short lowercase visual/style descriptor tags"], "category": "one likely domain category like Architecture, Interiors, Fashion, Art, Product Design"}',
                },
              ],
            },
          ],
        }),
      });

      const data = await anthropicRes.json();
      const text = data?.content?.[0]?.text || "{}";
      const cleaned = text.replace(/^```(json)?|```$/gm, "").trim();

      return new Response(cleaned, {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    } catch (err) {
      return new Response(JSON.stringify({ error: String(err) }), {
        status: 500,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }
  },
};
