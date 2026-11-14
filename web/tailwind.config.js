/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "media",
  theme: { extend: { fontFamily: { mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"] } } },
  plugins: [],
};
