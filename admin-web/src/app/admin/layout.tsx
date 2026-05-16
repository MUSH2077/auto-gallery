import Link from "next/link";

export const dynamic = 'force-dynamic';

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return (
    <div>
      <nav className="bg-slate-900 text-white px-6 py-3 flex items-center gap-6 text-sm flex-wrap">
        <Link href="/admin" className="font-bold text-base">auto-gallery</Link>
        <Link href="/admin" className="hover:text-gray-300">Dashboard</Link>
        <Link href="/admin/sources" className="hover:text-gray-300">Sources</Link>
        <Link href="/admin/creators" className="hover:text-gray-300">Creators</Link>
        <Link href="/admin/subscriptions" className="hover:text-gray-300">Subscriptions</Link>
        <Link href="/admin/downloads" className="hover:text-gray-300">Downloads</Link>
        <Link href="/admin/works" className="hover:text-gray-300">Works</Link>
        <Link href="/admin/tags" className="hover:text-gray-300">Tags</Link>
        <Link href="/admin/settings" className="hover:text-gray-300">Settings</Link>
      </nav>
      <div className="min-h-[calc(100vh-52px)]">{children}</div>
    </div>
  );
}
