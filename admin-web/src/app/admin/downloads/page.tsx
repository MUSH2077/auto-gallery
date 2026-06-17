'use client';
import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

export default function DownloadsRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace('/admin/jobs?tab=downloads');
  }, [router]);
  return null;
}
