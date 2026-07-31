import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Quant Lab｜本地策略工作台",
  description: "面向 Windows 本地运行的量化研究与回测界面。",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
