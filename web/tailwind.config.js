/** @type {import('tailwindcss').Config} */
// Custody Record: a light working console. Bond-white ground, carbon ink, a manila shell rail,
// stamp-violet for seals and focus, and state colours that only appear where they mean something.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    colors: {
      transparent: "transparent",
      current: "currentColor",
      white: "#FFFFFF",
      ground: "#F2F3EF", // bond white: the page
      paper: "#FBFBF8", // raised record surfaces (exhibits, tables, inputs)
      wash: "#E9EAE4", // the custody-chain band, table heads, hover
      ink: { DEFAULT: "#172033", 2: "#4A5468", 3: "#5E6678" }, // text: primary / secondary / tertiary
      rule: { DEFAULT: "#C9CCC4", strong: "#172033" }, // hairlines / ink rules
      manila: { DEFAULT: "#D9B46A", deep: "#C9A050", ink: "#4A3A16", hover: "#E2C07D" }, // shell rail
      stamp: { DEFAULT: "#5B3FA0", soft: "#ECE7F6", deep: "#45307D" }, // seals, signatures, focus
      seal: { DEFAULT: "#B4232F", soft: "#F7E3E4" }, // critical, refused, failed
      record: { DEFAULT: "#2A7247", soft: "#E1F0E6" }, // verified, ready, passed
      caution: { DEFAULT: "#8A5A00", soft: "#F6EBD3" }, // pending, degraded, insufficient
      info: { DEFAULT: "#2D5B8A", soft: "#E2ECF5" }, // insufficient data, neutral notices
    },
    fontFamily: {
      sans: ['"Public Sans Variable"', "system-ui", "sans-serif"],
      mono: ['"JetBrains Mono"', "ui-monospace", "monospace"],
    },
    fontSize: {
      // One scale for the whole console: 11 / 12 / 13 / 14 / 15 / 17 / 20 / 24 / 30
      "2xs": ["11px", { lineHeight: "16px", letterSpacing: "0.02em" }],
      xs: ["12px", { lineHeight: "17px" }],
      sm: ["13px", { lineHeight: "19px" }],
      base: ["14px", { lineHeight: "21px" }],
      md: ["15px", { lineHeight: "23px" }],
      lg: ["17px", { lineHeight: "25px" }],
      xl: ["20px", { lineHeight: "28px", letterSpacing: "-0.01em" }],
      "2xl": ["24px", { lineHeight: "31px", letterSpacing: "-0.015em" }],
      "3xl": ["30px", { lineHeight: "37px", letterSpacing: "-0.02em" }],
    },
    extend: {
      borderWidth: { 1.5: "1.5px" },
      boxShadow: {
        lift: "0 1px 2px rgba(23,32,51,.06), 0 6px 18px -6px rgba(23,32,51,.18)",
        sheet: "-12px 0 40px -12px rgba(23,32,51,.28)",
      },
      transitionTimingFunction: { out: "cubic-bezier(0.16, 1, 0.3, 1)" },
      keyframes: {
        sign: { from: { opacity: "0", transform: "translateY(4px)" }, to: { opacity: "1", transform: "none" } },
        stamp: {
          "0%": { opacity: "0", transform: "rotate(-2deg) scale(1.35)", filter: "blur(2px)" },
          "60%": { opacity: "1", transform: "rotate(-2deg) scale(.97)", filter: "blur(0)" },
          "100%": { opacity: "1", transform: "rotate(-2deg) scale(1)" },
        },
        sheet: { from: { transform: "translateX(24px)", opacity: "0" }, to: { transform: "none", opacity: "1" } },
        pulse: { "0%,100%": { opacity: "1" }, "50%": { opacity: ".35" } },
      },
      animation: {
        sign: "sign 420ms cubic-bezier(0.16,1,0.3,1) both",
        stamp: "stamp 520ms cubic-bezier(0.16,1,0.3,1) both",
        sheet: "sheet 320ms cubic-bezier(0.16,1,0.3,1) both",
        live: "pulse 1.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
