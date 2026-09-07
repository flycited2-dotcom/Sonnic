import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Sonnic — пространство для ваших снов",
  description: "Локальный прототип дневника и рефлексии о снах. Local dream reflection prototype.",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="ru"><body>{children}</body></html>;
}
