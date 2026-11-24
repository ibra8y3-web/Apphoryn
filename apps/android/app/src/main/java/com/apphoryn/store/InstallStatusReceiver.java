package com.apphoryn.store;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;
import android.widget.Toast;

public final class InstallStatusReceiver extends BroadcastReceiver {
    public static final String ACTION_INSTALL_STATUS = "com.apphoryn.store.INSTALL_STATUS";
    public static final String EXTRA_SESSION_ID = "apphoryn_session_id";

    @Override
    public void onReceive(Context context, Intent intent) {
        int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            Intent confirmation = intent.getParcelableExtra(Intent.EXTRA_INTENT);
            if (confirmation != null) {
                confirmation.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                context.startActivity(confirmation);
            }
            return;
        }

        String message;
        if (status == PackageInstaller.STATUS_SUCCESS) {
            message = "Apphoryn: Android finished installing the split package.";
        } else {
            String detail = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
            message = "Apphoryn installation failed: " + (detail == null ? "Android Package Installer rejected the package." : detail);
        }
        Toast.makeText(context.getApplicationContext(), message, Toast.LENGTH_LONG).show();
    }
}
