import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Sonnic — пространство для ваших снов",
  description: "Запишите сон, исследуйте его образы и найдите собственные ассоциации. Sonnic — пространство для спокойного размышления.",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="ru"><body>{children}</body></html>;
}
