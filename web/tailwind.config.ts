import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Geist Variable", "Geist", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["Geist Mono", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      boxShadow: {
        panel: "0 16px 48px -30px rgb(15 23 42 / 0.22)",
      },
      keyframes: {
        shimmer: {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(100%)" },
        },
        enter: {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        breathe: {
          "0%, 100%": { opacity: "0.55", transform: "scale(0.88)" },
          "50%": { opacity: "1", transform: "scale(1)" },
        },
      },
      animation: {
        shimmer: "shimmer 1.6s cubic-bezier(0.16, 1, 0.3, 1) infinite",
        enter: "enter 440ms cubic-bezier(0.16, 1, 0.3, 1) both",
        breathe: "breathe 2.8s cubic-bezier(0.16, 1, 0.3, 1) infinite",
      },
    },
  },
  plugins: [],
} satisfies Config;
