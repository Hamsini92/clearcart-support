import type { Metadata } from "next";
import { Manrope } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const manrope = Manrope({ subsets: ["latin"], variable: "--font-manrope" });

export const metadata: Metadata = {
  title: "ClearCart Support",
  description: "AI customer support agent for e-commerce refunds",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={manrope.variable}>
      <body>
        <header className="topbar">
          <div className="brand">
            <span className="brand-mark">ClearCart</span>
            <span className="brand-sub">Support</span>
          </div>
          <nav>
            <Link href="/chat">Customer Chat</Link>
            <Link href="/admin">Admin Dashboard</Link>
          </nav>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
