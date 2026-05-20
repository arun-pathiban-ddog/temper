import Link from "next/link";
import { AlertCircle } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

interface ErrorDisplayProps {
  title?: string;
  message: string;
  retry?: () => void;
  backHref?: string;
  backLabel?: string;
}

export default function ErrorDisplay({
  title = "Something went wrong",
  message,
  retry,
  backHref = "/",
  backLabel = "Back to Dashboard",
}: ErrorDisplayProps) {
  return (
    <div className="flex items-center justify-center min-h-[256px]">
      <div className="w-full max-w-md space-y-4">
        <Alert variant="destructive" className="rounded-[2px]">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>{title}</AlertTitle>
          <AlertDescription>{message}</AlertDescription>
        </Alert>
        <div className="flex items-center gap-2.5">
          {retry && (
            <Button size="sm" onClick={retry} className="rounded-[2px]">
              Retry
            </Button>
          )}
          <Button variant="secondary" size="sm" asChild className="rounded-[2px]">
            <Link href={backHref}>{backLabel}</Link>
          </Button>
        </div>
      </div>
    </div>
  );
}
