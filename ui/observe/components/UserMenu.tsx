"use client";

import Image from "next/image";
import { useSession, signOut } from "next-auth/react";
import { LogOut } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";

export default function UserMenu() {
  const { data: session } = useSession();

  if (!session?.user) return null;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" className="flex items-center gap-2 px-3 py-2 w-full h-auto justify-start text-xs text-[var(--color-text-secondary)] rounded-none">
          {session.user.image && (
            <Image
              src={session.user.image}
              alt=""
              width={20}
              height={20}
              className="rounded-full flex-shrink-0"
            />
          )}
          <span className="truncate max-w-[100px]">{session.user.name}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-48 rounded-[2px]">
        {session.user.email && (
          <>
            <div className="px-2 py-1.5 text-xs text-[var(--color-text-muted)] truncate">
              {session.user.email}
            </div>
            <DropdownMenuSeparator />
          </>
        )}
        <DropdownMenuItem
          onClick={() => signOut()}
          className="text-xs cursor-pointer rounded-[2px]"
        >
          <LogOut className="mr-2 h-3.5 w-3.5" />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
